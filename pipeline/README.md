# Локальный pipeline-agent — шаг 6

Агент реализован на C++ с `td::actor`, SHA-256 из `tdutils` и асинхронным HTTP-клиентом Boost.Beast. Он проверяет профиль, запускает локальный backend, выполняет health/warmup, следит за процессами и ограниченно останавливает их. Отдельный Python-процесс `simulator.py` предоставляет dev API через Unix socket. Python-зависимостей вне стандартной библиотеки нет.

Это основа локального lifecycle. Состояние `LOCAL_READY` означает успешный warmup **одного** симулятора. `group_ready` остаётся `false`, `epoch` — `null`, `hardware_attested` — `false`. Формирование группы, её полный warmup, WireGuard и подключение к Cocoon worker относятся к шагам 7–9. Здесь нет распределённой математики, tensor RPC, GPU inference или аппаратной аттестации.

## Сборка и проверка одной командой

Нужны настроенный CMake build Cocoon (например, `build/local`), C++20/Boost и Python 3.9+. См. [локальную сборку](../docs/deployment.md). На нативном Mac из корня репозитория:

```bash
python3 test/test-pipeline-agent.py --build-dir build/local
```

Команда собирает оба варианта агента и C++-тест профилей, выполняет интеграционные тесты и проверяет отсутствие оставшихся process groups и sockets. При ошибке возвращает ненулевой код. `--no-build` использует существующие бинарники. Нужен доступ к созданию локальных Unix sockets и управлению своими дочерними процессами; сетевой доступ в интернет и GPU не требуются.

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

`stop` подтверждает начало остановки; завершение подтверждают выход агента и финальный `status.json`. Для member используется `pipeline/profiles/simulator-member.json` и другой каталог. Агенты пока не соединяются друг с другом. `/v1/models` симулятора — API backend, не endpoint групповой readiness для worker.

## Доверенный профиль и runtime

Каталог разрешённых профилей находится в `Profile.cpp` и компилируется в агент. JSON в `profiles/` — только примеры **runtime-конфига**, а не источник доверенной policy. Сейчас реализован `simulator-dev-pp2-v1`: два логических rank, TP=1, DP=1, synthetic dtype, тестовая модель без dm-verity/GPU.

`pipeline-agent-dev` имеет отдельную compile-time policy. `pipeline-agent` собран с production policy и отклоняет dev-профиль до создания runtime-каталога или запуска процессов. Поддерживаемых production backend profiles на этом этапе ещё нет. Runtime-флагов `--no-tee`, `--dev`, произвольных executable/argv/env, OCI tags или внешнего каталога policy нет. Доверенный simulator запрещён и при попытке объявить его production-профилем, что отдельно проверяет C++-тест.

| Runtime-поле | Разрешённые значения |
|---|---|
| `profile` | `simulator-dev-pp2-v1` |
| `rank`, `role` | `0` / `head` или `1` / `member` |
| `limits.max_model_len` | Целое 16–512, default 512 |
| `limits.max_num_seqs` | Целое 1–2, default 2 |
| `limits.max_num_batched_tokens` | От `max_model_len` до 512, default 512 |
| `simulator.scenario` | Сценарий из таблицы ниже, default `normal` |
| `simulator.startup_delay_ms`, `simulator.warmup_delay_ms` | Целое 0–30000, default 0 |

Неизвестные поля, неверные типы, дубли ключей, несовместимые значения, конфиг больше 64 KiB или глубже 16 уровней отклоняются. `effective_config` содержит нормализованные defaults, модель, backend, policy, topology, capabilities и lifecycle budgets. SHA-256 вычисляется из JSON с сортированными ключами. Rank/role и локальная инъекция сбоев не входят в общий digest; параметры совместимости и лимиты входят. Hash подтверждает равенство конфигураций, а не аттестацию.

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

Дочерние процессы поддерживаемого backend не должны делать `setsid`/покидать группу. Здесь проверяются штатный stop, SIGINT/SIGTERM, обработанные ошибки агента и сбои backend. SIGKILL самого агента не позволяет выполнить его cleanup; Linux deployment должен дополнительно владеть всей cgroup через systemd/container runtime. Групповые leases, restart/backoff и cgroup/network isolation реализуются следующими шагами. POSIX-код в этом этапе проверен на macOS; Linux-проверка остаётся частью VM-этапа.

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

Пример добавления к runtime JSON: `"simulator": {"scenario": "warmup-hang"}`. Дополнительно запрос API может содержать dev-поле `simulator` с `fault` (`none`, `truncate`, `hang`, `http-error`, `error-event`) и `token_delay_ms` (0–1000). Эти сценарии нужны для будущих gate/integration tests и не исправляют отложенные шаги 3–5 существующего worker.
