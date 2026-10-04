# Шаг 12: воспроизводимый dev deployment

Дата: 2026-10-05 (Europe/Istanbul). Dev deployment прошёл первичную приёмку в VM
и отдельно для SGLang/vLLM на двух RTX. Исправления и оставшаяся проблема после
повторного ревью описаны ниже.
Инструкция запуска: [DEPLOYMENT.md](DEPLOYMENT.md).

## Что реализовано

`pipeline/deployment/bundle.py` создаёт согласованные комплекты для одного или двух
Linux-хостов. У группы два rank; backend — simulator, SGLang или vLLM. Комплект
содержит общий deployment ID, точный Docker image ID, закреплённый model catalogue,
profile, LAN endpoints, CPU/RAM/GPU assignments, agent и worker configuration.
Deployment ID входит в существующий membership digest: разные образы, настройки
и версии host tooling не образуют одну группу. Локальная проверка заново выводит
agent/worker config из общей конфигурации и отклоняет изменённые файлы до запуска.

Команды `check`, `start`, `wait`, `status`, `request`, `stop` не требуют ручной
правки внутренних конфигураций. Один и тот же launcher использует общие agent и
backend adapters при placement на одной VM или на двух физических GPU-хостах.
Сначала выполняется подготовка образа/модели с доступом к сети, затем serving из
того же образа и read-only model mount. Установленные binaries/helpers находятся
в `/opt/cocoon`; исходники, build cache и Docker socket в контейнер не передаются.

Внешний systemd unit владеет каждым rank. Контейнер имеет отдельную cgroup,
PID/mount namespaces, CPU/RAM limits и выбранную по PCI BDF → UUID GPU. `ExecStopPost`
работает и после SIGKILL host supervisor: удаляет контейнер, проверяет освобождение
cgroup и удаляет оставшийся принадлежащий rank интерфейс. Resource lease снимается
только после подтверждения очистки; повторный запуск с занятыми ресурсами запрещён.
Нормальный stop сохраняет диагностику и базы dev-сервисов. Повторный start активной
группы идемпотентен. Новый start сбрасывает старый READY до асинхронного запуска unit.

Agent/network helper работают от root внутри контейнера. Backend/API helper и
их потомки — UID/GID 65534, без capabilities, с NoNewPrivs, в engine namespace
с `lo` и WireGuard. Cocoon services — UID/GID 10001, без usable capabilities,
с NoNewPrivs, в underlay. Service UID не читает конфигурацию/control агента и не
открывает backend UDS в обход gate. Root filesystem read-only; JIT-библиотеки
PyTorch/Triton создаются в приватном каталоге epoch на исполняемом tmpfs `/run`.

Underlay имеет default-drop до старта агента и во время замены epoch guardian.
Разрешены peer control/WireGuard и, только для UID 10001, явно заданные TCP
destinations внешних dev proxy/key-manager. Engine не получает underlay route,
namespace FD, TCP/UDP/raw socket FD или container runtime socket. Именованные
health/backend UDS намеренно доступны агенту/gate через границу network namespace.
Модель,
capacity, gate endpoint и коэффициент цены worker генерируются вместе; rank count
не используется как множитель usage/цены.

## Воспроизведение

Подготовка образа и моделей, зависимости хоста, свободные IP и генерация комплекта
описаны в [инструкции](DEPLOYMENT.md). GPU snapshots и драйвер/runtime берутся из
[инструкции стенда](../experiments/gpu-pipeline/README.md). Тесты не устанавливают
пакеты и не меняют NVIDIA driver или Docker storage driver. Каждый integration
suite запускает собственные зависимости, делает assertions, выполняет cleanup
и возвращает ненулевой код при ошибке. Output-каталог должен быть новым.

```bash
python3 -B test/test-pipeline-deployment.py
cmake --build build/local --target test-pipeline-profile test-pipeline-sglang test-pipeline-vllm
build/local/pipeline/test-pipeline-profile
build/local/pipeline/test-pipeline-sglang
build/local/pipeline/test-pipeline-vllm

sudo python3 -B test/test-pipeline-deployment-linux.py \
  --config deployment.json --artifact /opt/cocoon-image-simulator/artifact.json \
  --service-ip 192.168.139.213 --output /opt/cocoon-deployment-test

python3 -B test/test-pipeline-deployment-gpu.py --backend vllm --model large \
  --lab experiments/gpu-pipeline/lab.json \
  --artifact-dir /home/ruslixag/cocoon-step12/image-vllm \
  --output build/deployment-vllm
python3 -B test/test-pipeline-deployment-gpu.py --backend sglang --model large \
  --lab experiments/gpu-pipeline/lab.json \
  --artifact-dir /home/ruslixag/cocoon-step12/image-sglang \
  --output build/deployment-sglang

python3 test/test-pipeline-worker.py --build-dir build/local
python3 benchmark/smoke-local.py --build-dir build/local --skip-build --scenario normal
```

GPU-команды выше предполагают, что **тот же image ID уже загружен на обоих
хостах**. Иначе передайте `--image-archive <путь-на-head-к-image.tar>`: suite сам
перенесёт архив. Два backend проверяются последовательно, поскольку используют
одни физические GPU. `test/deployment-sandbox.py` — read-only host-side auditor,
который integration suites вызывают после readiness для проверки реальных PID,
UID/GID, capabilities, namespace/cgroup и открытых дескрипторов.

## Стенд и артефакты

VM: Ubuntu 24.04 ARM64, OrbStack `cocoon-pipeline-net`, kernel `7.0.11-orbstack`,
Docker 29.1.3, systemd/cgroup v2. Два simulator rank на одном хосте: CPU 0–1 и 2–3,
по 1024 MiB RAM, дополнительные IP `192.168.139.211/24` и `.212/24` на `eth0`;
внешний service fixture — `.213`. Вложенная файловая система этой выделенной VM
потребовала Docker `vfs` вместо overlayfs; это особенность подготовки стенда,
а не настройка, которую deployment меняет на пользовательском хосте.

GPU: `hetzner-cuda-01` / `hetzner-cuda-02`, Ubuntu 26.04, kernel `7.0.0-34-generic`
/ `7.0.0-22-generic`, Docker 29.1.3, NVIDIA driver 595.91.07. На каждом хосте одна
RTX 4000 SFF Ada, 20475 MiB, BDF `0000:01:00.0`. Rank получает CPU 0–7, 24576 MiB
RAM и свою GPU; дополнительные IP `192.168.100.12/24` / `.13/24` на
`enp4s0.4000`. Проверка шага 12 использует Qwen3-14B revision
`40c069824f4251a91eefaf281ebe4c544efd3e18`, PP=2/TP=1, BF16, context 4096,
одну последовательность и CPU weight offload 0. Это bare metal, без VFIO;
simulator VM не использует GPU. Процедура GPU passthrough в VM остаётся в разделе 2
плана и связана с инструкцией deployment, но этот прогон её не квалифицирует.

| Runtime | Точный image ID |
|---|---|
| Simulator ARM64 | `sha256:9dff276a20e4f4a8754610473e91eadcc455dcf31a48de5cf204d8f23158a9d4` |
| vLLM AMD64 | `sha256:b3d56ace17aab1a7b45004c3e3185a5ff904355b5bcc2434fd1e9c760f0892db` |
| SGLang AMD64 | `sha256:b1384fb654b3b5b385f2e6372d0720f8d375faa6cc19feddd16538a8190edc78` |

GPU bases закреплены в `experiments/gpu-pipeline/profiles.json`: vLLM
`0.29.0+cu129`, SGLang `0.5.10.post1-cu130`. Simulator base:
`ubuntu@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55`.
Общий SHA-256 model catalogue:
`13ae6bd2b1bb759ba31c115ec71c306ebc46277151452fff26b862a3bf75b806`.
`artifact.json` и `runtime-files.sha256.json` сохраняют image/base ID и хеши
установленных файлов. Поле `source_sha256` здесь означает digest manifest runtime
payload; это не Git commit и не хеш всего checkout. Версии host CLI отдельно
включены в deployment ID и проверяются на обоих rank.

Для передачи GPU-образов на стенде использован вспомогательный
`test/sglang-gpu/export-deployment-image.py`: он исключает из `docker save`
слои **точного base**, уже загруженного на обоих хостах. После загрузки suite
проверяет полный image ID. Общая инструкция использует обычный полный архив;
приёмка не требует delta-формата.

## Результаты и покрытие критериев

Эти VM/GPU-прогоны относятся к версии до исправлений повторного ревью.

| Проверка | Результат | Артефакты |
|---|---|---|
| Linux VM, simulator PP=2 | PASS, 16 проверок, cleanup подтверждён | [vm-final/result.json](../build/step12/vm-final/result.json) |
| vLLM, Qwen3-14B, две RTX | PASS, 6 проверок, после stop 2 / 2 MiB | [gpu-vllm-r4/result.json](../build/step12/gpu-vllm-r4/result.json) |
| SGLang, Qwen3-14B, две RTX | PASS, 6 проверок, после stop 2 / 2 MiB | [gpu-sglang/result.json](../build/step12/gpu-sglang/result.json) |

Нативно прошли 10 bundle/config tests и C++ profile/adapter contracts. Отдельно
прошли 17 групп worker integration (`build/step12/worker-regression-r2`) и обычный
worker без pipeline: восемь API-вариантов normal smoke
(`build/step12/normal-worker`). Это проверка совместимости пользовательского пути,
а не новый single-GPU benchmark.

В `sandbox-0.json` / `sandbox-1.json` сохранены реальные host PID, команды,
UID/GID, capabilities, netns/cgroup и результат FD audit; это проверка работающих
процессов, а не только Docker launch flags. У vLLM проверены 5 процессов head
и 4 member, у SGLang — также 5 и 4. `ready.json`, `deployment.json`,
`artifact.json`, per-host bundles и `diagnostics-*.json` сохраняются рядом.
Для vLLM дополнительно сохранён независимый `cleanup-audit-*.json`: lease
отсутствует, cgroup пуста, интерфейс отсутствует, обе GPU используют 2 MiB.
VM `final-host-audit.json` подтверждает отсутствие всех контейнеров и leases
launcher после suite. Исходники изменённых C++ компонентов и все используемые
packaged Python helpers в трёх образах сверены с checkout на момент первичной
приёмки, до исправлений ниже; manifests находятся
в `build/step12/{simulator,sglang,vllm}-payload.json`. Копия `host.py` внутри
образа не используется: актуальный host CLI поставляется и проверяется bundle.
`build/step12/final-host-{0,1}.json` подтверждают конечное отсутствие контейнеров,
leases и GPU-процессов deployment на обоих физических хостах. Посторонний
`open-webui` на head сохранил состояние healthy.

Внешний fixture VM подтвердил обычный и encrypted запрос: usage 2 prompt +
4 completion = 6 tokens, total cost 12 в обоих случаях. Внешний fixture
сохранился после stop группы; затем suite удалил только собственные зависимости.
Проверки этого режима — [external/result.json](../build/step12/vm-final/external/result.json).

Проверки VM: неверный image/endpoint до старта, полный запрос через Cocoon,
идемпотентный start, устаревший READY, private PID/read-only mounts, права на
agent/control/backend sockets, изоляция backend; SIGKILL каждого агента,
одновременный SIGKILL agent/guardian, процесс вне process group, SIGKILL внешнего
supervisor, удержанный lease, частичный старт без peer и повторное развёртывание.
Отдельный внешний UID-10001 fixture обслуживает plaintext и encrypted запросы
через proxy/key-manager при default drop. Проверяются разрешённые служебные
порты, запрет открытого постороннего порта, client-порта и root UID, недоступность
underlay для engine. Stop группы сохраняет внешний fixture, затем suite очищает
свои зависимости. Правила host firewall сравниваются без динамических counters.

GPU-проверки: одинаковый image ID, реальный PP=2 запрос с usage через Cocoon,
private PID/read-only runtime/model, ровно назначенная GPU, host-side audit всех
процессов backend/helper, повторный start, SIGKILL head agent, GPU release и
полный запрос после повторного развёртывания. Полная матрица API/отмен/KV уже
квалифицирована отдельно в шагах 10–11; здесь проверяется новая упаковка и lifecycle.

## Найденное во время приёмки

- Вложенный `ip netns exec` пытался перемонтировать sysfs и не работал с private
  PID namespace. Переключение сети теперь использует `nsenter --net`, сохраняя
  PID/mount isolation. Возврата к host PID нет.
- При инкрементальной подготовке snapshot сохранённый старый mtime мог оставить
  устаревший agent binary. Копирование сравнивает bytes и обновляет mtime изменённых
  файлов; сборка дополнительно проверяет `deployment_id` свежим executable.
  Вендорный `lz4/build/cmake` сохраняется, исключён только корневой build cache.
- Первый vLLM deployment дошёл до загрузки модели и NCCL, но Triton не смог
  загрузить JIT `.so` с noexec tmpfs. `/run` теперь допускает исполнение, оставаясь
  приватным; для GPU восстановлены `memlock=-1` и 2 GiB shared memory.
- Повтор vLLM выявил устаревший READY в окне до запуска нового supervisor:
  `wait` мог завершиться до готовности Cocoon services, первый запрос получал 503.
  Host CLI теперь сбрасывает маркер до `systemctl start --no-block`; regression
  test специально оставляет старый READY перед новым start.
- В host-side auditor исправлена проверка service capabilities: setuid обнуляет
  usable sets, а NoNewPrivs запрещает получить привилегии через exec. Bounding
  ceiling сервиса сохраняется как диагностическое поле; у backend пусты все sets,
  включая bounding. Это исправление тестового условия, runtime policy не ослаблялась.
- Auditor также различает IP sockets и разрешённые private UDS: принятый
  `health.sock` наблюдался в Unix socket table пространства подключившегося
  агента. Разрешены только пути health/backend из текущего agent status; все
  underlay IP/raw sockets, namespace FD и остальные underlay Unix sockets
  по-прежнему приводят к ошибке проверки.

Неуспешные прогоны сохранены рядом с успешными в `build/step12` и в
`/opt/cocoon-step12-acceptance-r*` VM. Они не засчитываются как приёмка. После
неуспешных GPU-прогонов cleanup подтвердил освобождение обеих GPU до 2 MiB.

## Исправления после повторного ревью

Исправлены проблемы 2 и 3:

- Service READY теперь обновляется непрерывно и снимается при отрицательной
  проверке. Worker сообщает число живых proxy connections после handshake;
  историческое `sc_inited` больше не используется как свидетельство связи.
  Проверяются enabled/uplink/model identity, а в local-режиме — также список
  доступных workers для модели через client. Маркер имеет monotonic timestamp
  и TTL 3 секунды. Supervisor требует свежую проверку после перехода группы
  в ready или смены epoch. Первоначальный startup deadline сохраняется;
  последующая временная потеря service readiness допускает восстановление.
- Model catalogue digest для image label и `artifact.json` берётся из
  `/export/runtime/pipeline/sglang-models.json`, подготовленного из snapshot.
  Изменение живого checkout во время компиляции больше не меняет metadata
  уже собранного payload.

После исправлений прошли 19 Python tests: bundle/config, имитация конкурентного
изменения каталога при сборке, потеря/возврат readiness, expiry, смена epoch и
startup deadline. Пересобран `worker-runner`; на реальных локальных runners
с fake-TON прошли 7 сценариев: начальная готовность, disable/enable worker,
потеря/возврат backend, SIGKILL/restart proxy. При отключённом proxy `sc_inited`
оставался true, но live connection count становился нулём и READY снимался.
Оба service mode проверены; процессы и loopback-порты освобождены.
Артефакт: [readiness-review-fixes-r2/result.json](../build/step12/readiness-review-fixes-r2/result.json).

Воспроизведение новых проверок:

```bash
python3 -B test/test-pipeline-deployment.py
cmake --build build/local --target worker-runner proxy-runner client-runner key-manager-runner router cocoon-subst -j4
python3 -B test/test-pipeline-deployment-readiness.py \
  --build-dir build/local --output build/deployment-readiness
```

VM/GPU-прогоны после этих исправлений не повторялись; прежние image IDs не
содержат новый worker status и service supervisor. Для deployment нужен новый образ.

Проблема 1 из ревью остаётся открытой: `host.py` принимает любой ненулевой код
`ip -j link show` за отсутствие интерфейса. Ошибка проверки (например, отказ в
доступе) может снять resource lease без подтверждённого cleanup. В этом исправлении
cleanup не менялся; этот случай нужно устранить и добавить в Linux-проверки.

## За скобками реализации шага 12

Уже запланировано: отложенный **шаг 5** — ограничения очередей всего Cocoon-пути;
**шаг 13** — длительная нагрузка, TTFT/inter-token latency/throughput и издержки
WireGuard для обоих backend. Краткие lifecycle-прогоны не заменяют эти измерения.

Сохраняются открытые P3-02 (encryption readiness), P4-01/P4-02 (внешняя TL-отмена
и reconciliation после падения proxy), P7-01/P7-03/P7-04 (production trust,
аттестация, ротация и отзыв доверия) согласно их критериям в плане. Для production
нужны отдельные image/profile, реальный TON egress и проверка на CC-стенде;
фиксированные fake-TON identities и dev router policy для этого не предназначены.

Осознанные ограничения: два rank, IPv4 LAN, закреплённые Qwen3 text profiles,
одна GPU-последовательность, ручной запуск на каждом Linux-хосте. Dev-контейнеры
с capabilities root supervisor не обеспечивают TDX/GPU attestation и не являются
CVM provisioner. Другие placement, модели/версии, batching, LoRA, quantization,
оптимизации и production orchestration требуют отдельного решения о scope.

При первичной приёмке P7-02, P8-01, P8-02 и P9-01 отмечены закрытыми в рамках
dev-MVP; найденная при ревью ошибка cleanup требует дополнительного исправления
по описанному выше случаю. Production image/trust и реальный TON остаются
отдельной работой P7-01 и раздела 6 плана.
