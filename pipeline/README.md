# Pipeline-agent — supervisor, группа и WireGuard

Агент реализован на C++ с `td::actor`, SHA-256 из `tdutils` и асинхронным HTTP-клиентом Boost.Beast. Он проверяет профиль, запускает локальный backend, выполняет health/warmup, следит за процессами и ограниченно останавливает их. Отдельный Python-процесс `simulator.py` предоставляет dev API через Unix socket. Python-зависимостей вне стандартной библиотеки нет.

Без секции `group` агент работает как локальный supervisor шага 6: `LOCAL_READY` означает warmup **одного** симулятора, `group_ready=false`, `epoch=null`. С секцией `group` включается протокол шага 7: два агента согласуют конфигурацию по mutual TLS и переходят в `READY` после warmup обоих симуляторов. Профиль `simulator-dev-pp2-wg-v1` добавляет настоящий WireGuard и изоляцию сети в Linux, шаг 8. Во всех этих режимах `hardware_attested=false`. Здесь нет распределённой математики, tensor RPC, GPU inference или аппаратной аттестации. Подключение к Cocoon worker остаётся в шаге 9.

В целевом CVM deployment агент работает внутри **каждой** CVM. Head CVM содержит `worker-runner`, агент-координатор и rank 0 движка; member CVM — агент и rank 1. Отдельная CVM для координатора не нужна. Нативные проверки ниже запускают эти роли обычными процессами на Mac.

## Сборка и проверка одной командой

Нужны настроенный CMake build Cocoon (например, `build/local`), C++20/Boost и Python 3.9+. См. [локальную сборку](../docs/deployment.md). На нативном Mac из корня репозитория:

```bash
python3 test/test-pipeline-agent.py --build-dir build/local
python3 test/test-pipeline-group.py --build-dir build/local
```

Первая команда проверяет локальный supervisor, вторая — формирование группы. Они собирают необходимые бинарники, выполняют C++ и интеграционные тесты и проверяют отсутствие оставшихся process groups и sockets. При ошибке возвращают ненулевой код. `--no-build` использует существующие бинарники. Нужны локальные Unix/TCP sockets и управление своими дочерними процессами; интернет и GPU не требуются.

Логи и `report.json` с результатами, платформой и SHA-256 исходников сохраняются в напечатанном `/tmp/cocoon-agent-*`. Каждый запуск использует новый каталог. Проверки включают:

- head/member как два независимых локальных агента с одинаковым `config_digest`;
- readiness только после warmup, JSON/SSE, usage и отмену генерации по disconnect;
- лимиты context/concurrency/token budget и освобождение квот после отмены;
- ошибку старта, startup/warmup timeout, ошибку warmup, зависание health и падение backend;
- stop во время старта/warmup, SIGINT/SIGTERM, повторный stop и новые запуски;
- остановленный через SIGSTOP backend и потомка, игнорирующего SIGTERM, в том числе после смерти родителя;
- зависший клиент status socket, сохранение чужих файлов и отказ повторно использовать каталог;
- заполнение всех 16 API-соединений зависшими запросами без срабатывания health watchdog;
- строгую схему, неизвестные параметры, dev-профиль в production executable и simulator в production policy.

## Запуск вручную

```bash
cmake --build build/local --target pipeline-agent pipeline-agent-dev -j 4
build/local/pipeline/pipeline-agent-dev --config pipeline/profiles/simulator-head.json --check-config
build/local/pipeline/pipeline-agent-dev --config pipeline/profiles/simulator-head.json --run-dir /tmp/cocoon-pipeline-head
```

`--run-dir` должен указывать на **новый** каталог, родитель которого уже существует. Для следующего запуска выберите новый путь. Каталог имеет права `0700`, sockets — `0600`. Длинные пути отклоняются из-за ограничения Unix sockets на macOS. Агент работает в foreground; Ctrl+C и SIGTERM запускают cleanup.

Из второго терминала:

```bash
python3 pipeline/control.py /tmp/cocoon-pipeline-head status
curl --unix-socket /tmp/cocoon-pipeline-head/health.sock http://localhost/health
curl --unix-socket /tmp/cocoon-pipeline-head/backend.sock http://localhost/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"cocoon-simulator@v1:dev-fixture","messages":[{"role":"user","content":"hello"}],"max_tokens":2,"stream":true}'
python3 pipeline/control.py /tmp/cocoon-pipeline-head stop
```

`stop` подтверждает начало остановки; завершение подтверждают выход агента и финальный `status.json`. Для member используется `pipeline/profiles/simulator-member.json` и другой каталог. Эти конфиги запускают независимые локальные агенты. `/v1/models` симулятора — API backend, не endpoint групповой readiness для worker.

## Два агента с mutual TLS — шаг 7

В двух терминалах, сначала member, затем head:

```bash
build/local/pipeline/pipeline-agent-dev --config pipeline/profiles/group-member.json --run-dir /tmp/cpg-member
build/local/pipeline/pipeline-agent-dev --config pipeline/profiles/group-head.json --run-dir /tmp/cpg-head
```

Запустите head в течение четырёх секунд после member либо во время следующей попытки. Используйте новые runtime-каталоги. Конфиги задают один loopback TCP endpoint `127.0.0.1:12310`; адрес служит для соединения, identity проверяется независимо через TLS. Без согласования группы backend не запускается. Проверка и остановка:

```bash
python3 pipeline/control.py /tmp/cpg-head status
python3 pipeline/control.py /tmp/cpg-member status
python3 pipeline/control.py /tmp/cpg-head stop
```

В статусах должны совпадать `epoch`, `config_digest`, `group.group_id`, `group.roster_digest` и полный `group.roster`; после warmup — `group_ready=true`. `local_state` отражает локальный supervisor. API/health sockets и логи backend находятся в `e0/`, при восстановлении — в `e1/`, `e2/`. Остановка head закрывает оба backend; локальная остановка member закрывает его и вызывает отказ группы у head.

Протокол: `Hello` с взаимными challenge → `Prepare` с полным roster → `Commit` → локальные health/warmup и heartbeat → `Ready`. Roster связывает rank, role, проверенные TLS public key/image hash, boot ID, network public key и overlay IP. Head допускает только совпадающие profile/backend/config digest, member проверяет свою запись и закреплённого head. Дубли rank, identity, boot ID, network key или адреса отклоняются. X25519 private keys создаются внутри агента и остаются в памяти. У показанного здесь нативного профиля сеть симулируется (`network_setup=simulated`); Linux-профиль ниже использует эти ключи для настоящего туннеля.

Control transport — TLS 1.3 с проверкой обеих сторон, без session resumption. Используются существующие `generate_cert_and_key`, `RATLSPolicy` и `RATLSVerifyCallbackBuilder` Cocoon. **Dev evidence синтетическое и доступно любому разработчику**: verifier проверяет marker, непустой allowlist тестового image hash и криптографическую привязку claims к TLS key, но не CPU/GPU. Production policy отвергает эти удостоверения; разрешённых production profiles пока нет. Отдельный C++-тест проверяет именно TLS handshake с production policy, дополнительно к отказу production executable принять dev-конфиг.

Сертификат по умолчанию генерируется в RAM, действует час и закреплён на epoch. TLS-сессия закрывается за пять секунд до истечения локального или удалённого сертификата. Между epoch dev-агент выдаёт себе новую identity; `boot_id` сохраняется до перезапуска самого агента. Для негативных тестов `pipeline-dev-cert NEW_BASE [valid|wrong-image|wrong-key|missing-evidence|expired|short-lived]` создаёт файлы `NEW_BASE_cert.pem` / `NEW_BASE_key.pem` с правами `0600`, которые можно выбрать через `group.certificate_base`. Они перечитываются при новой попытке; просроченный fixture требует замены. Это отдельный dev helper, не обход production policy.

Frames имеют 4-байтовую big-endian длину и JSON до 64 KiB: version, kind, op, epoch, config digest, sequence, request ID, payload. Проверяются схема, типы, дубли ключей и глубина. Очередь записи ограничена восемью frames / 256 KiB, незавершённые handshakes — четырьмя, connect/handshake/frame — двумя секундами. Работа IO за один actor tick ограничена, чтобы поток сообщений не блокировал watchdog.

Повтор идентичного `Prepare`, `Commit` или `Stop` возвращает сохранённый ответ без повторного действия и **не продлевает lease**. Другой payload с тем же lifecycle request ID, старый sequence или чужой epoch отклоняется. После фиксации участника новые соединения не заменяют его. Все callbacks принадлежат объекту конкретного epoch.

| Бюджет формирования группы | Значение |
|---|---:|
| Heartbeat | 200 ms |
| Lease | 1500 ms |
| Formation и warmup группы | 4000 ms |
| Backoff после подтверждённой очистки | 500 ms |
| Повторные попытки после первой | 2 |

Потеря control-связи/lease или отказ локального backend закрывает readiness и останавливает оба backend. Новая попытка разрешена только после подтверждённого локального cleanup, получает новый epoch, roster и network keys. Нет выбора нового head, продолжения генерации или переноса KV-cache. При исчерпании попыток агент завершается с ошибкой. `READY` здесь подтверждает согласование и warmup двух **независимых симуляторов**, а не передачу тензоров или сквозную генерацию настоящего PP engine.

`test/test-pipeline-group.py` сохраняет `membership-unit.log`, логи агентов и `report.json` с SHA-256 в `/tmp/cpg-*`. Проверки охватывают успешный запуск/stop, ошибки identities и config, запрещённый dev evidence в production policy, дубли rank/ключей, replay и старые epoch, отказ warmup, падение backend, потерю member и паузу head, ограниченные retries, отсутствие клиентского сертификата, oversized frame и истечение действующей TLS-сессии. Проверяются новые epoch/keys и отсутствие созданных процессов/sockets после cleanup.

## WireGuard и Linux VM — шаг 8

`simulator-dev-pp2-wg-v1` работает от root **в отдельном underlay namespace внутри Linux VM**. Начальный network namespace VM отклоняется до настройки сети. Каждый агент владеет своим underlay; профиль не подходит для общей сетевой среды с другими сервисами. На Mac возможен `--check-config`, но попытка запуска этого профиля отклоняется до создания runtime-каталога. Нативный профиль шага 7 сохраняется.

Одна команда проверки в подготовленной Linux VM, из корня checkout:

```bash
sudo python3 test/test-pipeline-network.py --build-dir /opt/cocoon-build
```

Она собирает агент, создаёт два уникальных underlay namespace с адресами из `198.18.0.0/16`, поднимает head/member, проверяет настоящий трафик и отказы, затем проверяет cleanup перед удалением тестового стенда. Отчёт `report.json`, статусы, логи, `evidence.json` и `underlay.pcap` остаются в напечатанном `/tmp/cpn-*`. `--no-build` использует готовую сборку, `--test test_encrypted_backend_traffic_and_isolation` выбирает один сценарий. SHA-256 исходников включены в отчёт. Firewall начального namespace VM не изменяется; его неизменность проверяется.

Для воспроизведения на Mac использована отдельная ARM64 Ubuntu 24.04 VM OrbStack с kernel WireGuard. Подготовка выполняется один раз (имя VM должно быть новым):

```bash
orbctl create --isolated --isolate-network \
  --mount "$PWD:/work/cocoon" --memory 6G --cpus 4 --disk 24G \
  ubuntu:24.04 cocoon-pipeline-net
orbctl run -m cocoon-pipeline-net -u root apt-get update
orbctl run -m cocoon-pipeline-net -u root apt-get install -y \
  build-essential clang cmake ninja-build pkg-config git python3 \
  libboost-all-dev libssl-dev zlib1g-dev liblz4-dev libzstd-dev \
  libjemalloc-dev libsodium-dev libreadline-dev libsecp256k1-dev \
  iproute2 wireguard-tools nftables tcpdump util-linux
orbctl run -m cocoon-pipeline-net -u root git config --global --add safe.directory /work/cocoon
orbctl run -m cocoon-pipeline-net -u root git config --global --add safe.directory /work/cocoon/ton
orbctl run -m cocoon-pipeline-net -u root cmake \
  -S /work/cocoon -B /opt/cocoon-build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
  -DTON_ONLY_TONLIB=ON -DTON_USE_ABSEIL=OFF -DTON_USE_ROCKSDB=ON -DTON_USE_JEMALLOC=ON
```

После подготовки одна команда с Mac:

```bash
orbctl run -m cocoon-pipeline-net -u root python3 \
  /work/cocoon/test/test-pipeline-network.py --build-dir /opt/cocoon-build
```

Нужны инициализированные submodules Cocoon, доступ к build-зависимостям и kernel с namespaces/WireGuard/nftables. GPU для этой проверки не нужен. Linux build размещён на диске VM, отдельно от macOS build. Эта VM и её shared checkout — dev-стенд, а не CVM или доверенный production image.

### Сеть и границы привилегий

После `Prepare` добавляется идемпотентный `ConfigureNetwork`. Каждый агент передаёт **свой** приватный ключ локальному `network-helper.py` через анонимный pipe. Координатор получает только публичный ключ member; ключ не попадает в argv, environment, status, логи или key file. Helper передаёт конфигурацию `wg setconf` через stdin. Core dumps агента/helper/backend запрещены. Приватные ключи создаются заново для каждого epoch.

Helper создаёт `wg` в underlay и переносит его в новый `cp-<random>` namespace с именем `wg0`. UDP socket остаётся в underlay согласно [механизму WireGuard](https://www.wireguard.com/netns/). В engine namespace — только `lo` и `wg0`, адрес rank `10.231.0.1/32` или `.2/32`, один маршрут к другому rank, один peer с `AllowedIPs=<peer>/32`. Default route и открытого интерфейса между underlay и engine нет.

Firewall имеет default drop для input/output/forward в обеих средах. В underlay разрешены loopback и связь только с согласованным peer: TLS control `12310/TCP`, WireGuard `51820/UDP`. В engine разрешены loopback и обмен roster-адресов через `wg0`. Endpoint входит в аутентифицированный roster и должен совпадать с runtime placement; runtime не может задавать произвольные ключи, маршруты, MTU или правила firewall.

`pipeline-backend-sandbox` — обычный root-launched executable, без setuid. Он входит в готовый namespace, закрывает namespace FD, сбрасывает UID/GID на 65534, supplementary groups и все capabilities, устанавливает `no_new_privs`, запрещает core dump и связывает жизнь непосредственного backend с агентом через `PDEATHSIG`. Backend/потомки наследуют эту сеть; изменить маршруты или войти обратно в underlay они не могут. API/health остаются filesystem UDS, TCP API не публикуется. Только для синтетического тестового трафика simulator слушает overlay TCP 29999; это bounded echo, не tensor RPC.

Runtime-каталог агента в этом профиле root-owned `0711`, status/control — `0600`. Каталог epoch принадлежит backend (UID 65534) с правами `0700`, sockets — `0600`. Все родительские каталоги должны позволять backend пройти к epoch; VM-тест создаёт такие пути сам. Root-агент обращается к UDS через общий filesystem. Изоляция mount/PID/device и пользователь реального gate относятся к последующему deployment, а не к этому dev sandbox.

### Readiness, отказы и cleanup

Backend запускается после `Commit`, который допускается только после двустороннего обмена случайными challenge/response через WireGuard. Проба использует UDP 29998 в overlay каждые 200 ms, initial budget 15 s, после первого ответа timeout 2 s. Наличие интерфейса само по себе не даёт readiness. Проверка интерфейсов, MTU, маршрутов, public key/AllowedIPs и firewall выполняется раз в 400 ms. Агент проверяет также жизнь helper и его status watchdog (4 s). Для этого профиля heartbeat control — 500 ms, lease — 5 s, formation — 30 s; лимит двух повторных попыток и backoff 500 ms сохранены.

Удаление `wg0`, изменение проверяемой конфигурации, потеря data/control или выход helper закрывают группу. Сначала агент останавливает backend, затем helper удаляет `wg0` вместе с kernel keys **до** удаления namespace mount, удаляет свои firewall tables и запись владения. Даже удержанный другим процессом namespace FD не сохраняет старый WireGuard interface. Новый epoch допускается только после подтверждённого cleanup процессов и сети.

Helper следит за EOF owner pipe; потеря агента запускает сетевой cleanup. При SIGKILL helper живой агент сначала подтверждает остановку его process group, затем запускает ограниченный recovery helper без приватного ключа. Root-only запись `/run/cocoon-pipeline-net/cp-<random>` содержит cookie владения и inode underlay, записывается до первой сетевой мутации и удаляется после cleanup. Recovery не удаляет ресурс без совпадающей записи. При неподтверждённой очистке агент завершается с ошибкой без restart. Бюджеты: основной helper — 5 s graceful + 2 s kill; recovery — 6 s + 2 s kill. Каталог `/run/cocoon-pipeline-net` может оставаться пустым.

VM-тест проверяет bidirectional TCP echo (по 32 KiB с каждой стороны), отсутствие plaintext marker и постороннего IPv4-трафика на underlay во время READY, реальные UID/capabilities/netns/routes, запрет underlay-доступа в том числе к специально созданному слушающему сервису, запрет смены маршрута/namespace, разрывы data/control, удаление `wg0`, изменение firewall, SIGKILL helper и ошибку частичной настройки при занятом WG-порту. Проверяются новые epoch/ключи, очистка старых interfaces/namespaces/tables/owner records и отсутствие оставшихся процессов/sockets. Capture дополняет эти проверки; он не доказывает аппаратную конфиденциальность.

### Что осталось после шага 8

Worker gate/admission и реальный запрос Cocoon — шаг 9; SGLang/vLLM и GPU transport — шаги 10–11; контейнерные/device ограничения и развёртывание — шаг 12; нагрузка и LAN/MTU-проверки — шаг 13. Production attestation — P7-01 и Gate E. `P7-02` остаётся открытым: EOF/PDEATHSIG помогают при гибели агента, но остановка всей cgroup, включая потомков, покинувших process group, внешним supervisor здесь не доказана. Конкретные непокрытые задачи и условия возврата ведутся в [плане](../pipeline-plan.md#за-скобками-реализации-шага-8).

## Доверенный профиль и runtime

Каталог разрешённых профилей находится в `Profile.cpp` и компилируется в агент. JSON в `profiles/` — только примеры **runtime-конфига**, а не источник доверенной policy. Реализованы нативный `simulator-dev-pp2-v1` и Linux `simulator-dev-pp2-wg-v1`: два логических rank, TP=1, DP=1, synthetic dtype, тестовая модель без dm-verity/GPU. Примеры placement Linux-профиля — `profiles/network-head.json` и `profiles/network-member.json`; адреса должны существовать в выделенных underlay namespaces.

`pipeline-agent-dev` имеет отдельную compile-time policy. `pipeline-agent` собран с production policy и отклоняет dev-профиль до создания runtime-каталога или запуска процессов. Поддерживаемых production backend profiles на этом этапе ещё нет. Runtime-флагов `--no-tee`, `--dev`, произвольных executable/argv/env, OCI tags или внешнего каталога policy нет. Доверенный simulator запрещён и при попытке объявить его production-профилем, что отдельно проверяет C++-тест.

| Runtime-поле | Разрешённые значения |
|---|---|
| `profile` | `simulator-dev-pp2-v1` или `simulator-dev-pp2-wg-v1` |
| `rank`, `role` | `0` / `head` или `1` / `member` |
| `limits.max_model_len` | Целое 16–512, default 512 |
| `limits.max_num_seqs` | Целое 1–2, default 2 |
| `limits.max_num_batched_tokens` | От `max_model_len` до 512, default 512 |
| `simulator.scenario` | Сценарий из таблицы ниже, default `normal` |
| `simulator.startup_delay_ms`, `simulator.warmup_delay_ms` | Целое 0–30000, default 0 |
| `group.peer_port` | Только head, TCP 1024–65535; WG-профиль: только 12310 |
| `group.listen_port` | Только member, TCP 1024–65535; WG-профиль: только 12310 |
| `group.peer_host`, `group.listen_host` | Нативный профиль: только `127.0.0.1`; WG: соответствующий `network.peer_ip` / `underlay_ip`, это же default |
| `group.certificate_base` | Необязательный путь к dev fixture; default — сертификат в RAM |
| `network.underlay_ip`, `network.peer_ip` | Только WG-профиль: обязательные разные unicast IPv4 вне loopback/overlay; обязательна также секция `group` |

Неизвестные поля, неверные типы, дубли ключей, несовместимые значения, конфиг больше 64 KiB или глубже 16 уровней отклоняются. `effective_config` содержит нормализованные defaults, модель, backend, policy, topology, capabilities и lifecycle budgets. SHA-256 вычисляется из JSON с сортированными ключами. Rank/role, endpoints, certificate base и локальная инъекция сбоев не входят в общий digest; параметры совместимости, лимиты и membership policy входят. Hash подтверждает равенство конфигураций, а не аттестацию.

Симулятор считает токенами слова, разделённые whitespace; это тестовая семантика, не tokenizer настоящей модели. Ограничивает запрос 8192 байтами и 64 output tokens. Concurrency и сумма prompt tokens активных запросов ограничены профилем; token reservation удерживается до завершения/отмены запроса. Реального batching или GPU scheduler здесь нет.

Adapter строит фиксированный запуск Python с `-I -u`, путём bundled simulator, проверенными аргументами и отдельным минимальным environment. Пути Python и simulator задаются при CMake configure; runtime их не меняет. Это dev build из checkout: перенос бинарника в measured deployment и real model verification относятся к следующим этапам.

## Lifecycle и границы supervisor

```text
STARTING → WARMING → LOCAL_READY → STOPPING → STOPPED
     любая ошибка ───────────────→ STOPPING → FAILED
```

| Бюджет dev-профиля | Значение |
|---|---:|
| Startup | 3000 ms |
| Warmup | 2000 ms |
| Одна health probe | 500 ms |
| Интервал health probes | 200 ms |
| Отсутствие успешной health probe после readiness | 1500 ms |
| Graceful stop | 500 ms |
| Подтверждение cleanup после SIGKILL | 2000 ms |

Агент не ждёт синхронно ответа backend: control socket и watchdog продолжают работать во время зависшей probe. При stop текущая probe отменяется закрытием соединения. Статус сохраняется атомарно и содержит состояние, rank, digest, PID/PGID, код выхода, результат cleanup и причину ошибки. Закрытый control socket означает завершение процесса; финальный статус остаётся в файле.

Health probes используют отдельный `health.sock` с собственными обработчиками и лимитом соединений. Он принимает только `GET /health`; генерация и warmup идут через `backend.sock`. Поэтому зависшие API-клиенты не блокируют watchdog. Оба сокета имеют права `0600` и удаляются агентом после cleanup backend.

Backend запускается через `posix_spawn` в собственной process group. При остановке SIGTERM получает вся группа, затем SIGKILL гарантирует остановку оставшихся потомков. Leader остаётся waitable до последнего сигнала группе, чтобы исключить повторное использование его PID. Перед успехом проверяются reap leader и исчезновение группы. При неподтверждённом cleanup результат — ошибка; автоматического restart поверх неизвестного состояния нет. Логи и runtime-каталог сохраняются, удаляются только созданные sockets.

Дочерние процессы поддерживаемого backend не должны делать `setsid`/покидать группу. Здесь проверяются штатный stop, SIGINT/SIGTERM, обработанные ошибки агента и сбои backend. SIGKILL самого агента не позволяет выполнить его cleanup; Linux deployment должен дополнительно владеть всей cgroup через systemd/container runtime (P7-02). Групповые leases и restart/backoff работают при наличии секции `group`. POSIX-код проверяется на macOS и Linux; WireGuard network isolation описана выше, полное владение cgroup остаётся открытой задачей deployment.

## Воспроизводимые сбои симулятора

| `simulator.scenario` | Поведение |
|---|---|
| `normal` | Health, warmup и API работают |
| `startup-exit` | Выход с кодом 23 до открытия socket |
| `startup-hang` | Startup ждёт дольше deadline |
| `warmup-error` | Warmup возвращает HTTP 500 |
| `warmup-hang` | Health отвечает, warmup зависает |
| `health-hang` | После warmup зависают health probes |
| `crash-after-ready` | Процесс выходит с кодом 23 после warmup |
| `stubborn-child` | Создаётся потомок, игнорирующий SIGTERM |

Пример добавления к runtime JSON: `"simulator": {"scenario": "warmup-hang"}`. Дополнительно запрос API может содержать dev-поле `simulator` с `fault` (`none`, `truncate`, `hang`, `http-error`, `error-event`) и `token_delay_ms` (0–1000). Эти сценарии нужны для gate/integration tests и не заменяют отдельные проверки HTTP lifecycle и лимитов существующего worker в шагах 3–5.
