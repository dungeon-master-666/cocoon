# Развёртывание pipeline в dev-режиме

Эта инструкция относится к шагу 12.
Комплект запускает одну группу из двух rank с simulator, SGLang или vLLM.
Внешне группа представлена одним Cocoon worker. Для проверки используются
публичные тестовые ключи и fake-TON; это dev-стенд без аппаратной конфиденциальности.

## Что понадобится

- Linux с systemd, cgroup v2, Python 3.9+, Docker, `iproute2`, `util-linux`,
  `iputils-arping`. Команды управления выполняются через `sudo`.
- Для simulator: минимум четыре CPU и около 4 GiB свободной RAM в Linux VM.
  Для GPU: два совместимых NVIDIA GPU, драйвер и NVIDIA Container Toolkit;
  квалифицированы RTX 4000 SFF Ada 20 GiB и закреплённые Qwen3 profiles.
- Одна IPv4 LAN, два **свободных дополнительных IP**, по одному на rank.
  IP хостов использовать нельзя. LAN должна пропускать ipvlan и WireGuard
  UDP 51820, служебный TCP 12310 между rank. Имена интерфейсов могут различаться.
- CPU, RAM и GPU в конфигурации должны быть доступны. Для двух rank на одном
  хосте нужны разные CPU sets и разные GPU. BDF одинакового вида на разных
  физических хостах допустим. Владелец CPU/RAM здесь — контейнер с cgroup limits;
  GPU выделяется контейнеру по UUID, найденному из указанного PCI BDF.
  Launcher не допускает пересечения ресурсов между своими deployment. Нагрузку
  от других программ администратор хоста контролирует отдельно.

На Ubuntu зависимости хоста:

```bash
sudo apt-get update
sudo apt-get install -y docker.io python3 iproute2 util-linux iputils-arping
```

Если запускаете GPU внутри VM, сначала выполните процедуру passthrough из
[раздела 2 плана](../pipeline-plan.md#перевод-gpu-в-pcie-passthrough).
На физическом хосте устройство принадлежит VFIO, внутри guest — NVIDIA;
в deployment JSON указывается **guest BDF**. Не назначайте одну GPU двум VM
или одновременно VM и host-контейнеру. На bare metal VFIO не требуется.

## 1. Подготовить образ и модель

Из checkout с инициализированными submodules на Linux нужной архитектуры:

```bash
sudo python3 -B pipeline/deployment/build-image.py \
  --backend vllm --build-dir /opt/cocoon-build-vllm \
  --output /opt/cocoon-image-vllm --save
```

Для SGLang замените `vllm` на `sglang`; для VM без GPU — на `simulator`.
Сборка использует отдельный build cache и закреплённый backend base image.
В каталоге результата появятся `artifact.json`, `image.tar` и список хешей
файлов runtime. Образ содержит binaries, helpers и dev service templates;
при запуске исходники и build cache не монтируются.
Digest каталога в `artifact.json` и image label вычисляется из собранного runtime.
Если каталог в checkout изменился во время сборки, bundle отклонит его несовпадение
с артефактом: для нового каталога нужно заново подготовить образ.

Сборка и скачивание модели — отдельная фаза с доступом к сети. Подготовьте
snapshot модели по [инструкции GPU-стенда](../experiments/gpu-pipeline/README.md).
Путь из `model_root` должен содержать `Qwen3-0.6B/<revision>` или
`Qwen3-14B/<revision>` из `pipeline/sglang-models.json`. Runtime монтирует его
read-only в `/models` и сверяет содержимое по закреплённому manifest.

На втором хосте загрузите **тот же** архив:

```bash
sudo docker load -i image.tar
```

Не собирайте второй образ независимо: bundle проверяет точный image ID.
На первом хосте образ уже загружен. Имена Docker tags не используются при запуске.

## 2. Создать комплекты для хостов

Скопируйте `pipeline/deployment/example-gpu.json` в свой `deployment.json`.
Для simulator используйте `example-simulator.json`. Поменяйте hostname
(`hostname`), интерфейс (`ip addr`), свободные IP, CPU/RAM и GPU BDF
(`nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader`).
Выберите `backend` и `model`: `small` — Qwen3-0.6B, `large` — Qwen3-14B.
Simulator принимает только `model: simulator`.

```bash
python3 -B pipeline/deployment/bundle.py \
  --config deployment.json --artifact /opt/cocoon-image-vllm/artifact.json \
  --output bundles
```

Передайте целиком `bundles/head` первому хосту, `bundles/member` второму.
Для placement на одной машине получится один каталог с обоими rank.
Каталог вывода должен быть новым. Внутренние `agent-*.json` и `services.json`
не редактируйте: они выводятся из общей конфигурации и проверяются при запуске.
Имя модели, capacity, forwarding endpoint и цена задаются согласованно.
`coefficient` относится ко всему worker; usage не умножается на число rank.

## 3. Запустить и проверить

На каждом хосте из его каталога bundle:

```bash
sudo ./pipeline-deploy check
sudo ./pipeline-deploy start
sudo ./pipeline-deploy wait
```

Сначала выполните `start` на обоих хостах, затем `wait`. `start` возвращает
сразу, чтобы не мешать ручному запуску второго участника. Если peer не появился
до `startup_timeout`, локальный rank завершится с ошибкой и освободит ресурсы.
Повторный `start` уже работающего комплекта ничего не дублирует.

На head `READY` требует готовую группу, enabled worker с рабочим uplink и живым
соединением с proxy после handshake; в `local-fake-ton` дополнительно проверяется
объявление модели через client. Проверки продолжаются после старта. Ошибка проверки
снимает service READY, а маркер без обновлений истекает через 3 секунды; supervisor
возвращается в `STARTING` до восстановления. После смены epoch требуется проверка,
начатая уже после готовности новой группы. `startup_timeout` ограничивает выход
на первую полную готовность, а не восстановление после временного сбоя сервисов.
Эта версия требует пересборки образа: worker должен выдавать новое поле
`status.ready_proxy_connections` в `/jsonstats`.

На head отправьте тестовый запрос через Cocoon client → proxy → worker → pipeline:

```bash
sudo ./pipeline-deploy request --prompt 'Say hello.'
sudo ./pipeline-deploy status
```

В режиме `local-fake-ton` client, proxy и key-manager находятся внутри head.
Их API не публикуются в LAN. `request` обращается к loopback API внутри контейнера.

## 4. Остановить или разобраться с ошибкой

На **каждом** хосте:

```bash
sudo ./pipeline-deploy stop
```

Команда подтверждает cleanup; повторный stop допустим. systemd `ExecStopPost`
удаляет только контейнер и сетевые ресурсы своего deployment/rank, в том числе
после SIGKILL supervisor. Контейнерная cgroup и private PID namespace ограничивают
всё дерево процессов. Пока очистка не подтверждена, resource lease сохраняется
и повторный запуск с этими ресурсами отклоняется.
После устранения причины ошибки выполните `stop`, затем `start` и `wait`
на обоих хостах. Автоматического перезапуска unit после аварии нет.

`status` показывает каталог диагностики:
`/var/lib/cocoon-pipeline/<deployment-id>/rank-N`. Там остаются `agent.log`,
`services.log`, service logs/databases и состояния supervisor/cleanup.
Журнал systemd: `journalctl -u cocoon-pipeline-<первые-20-символов-id>-N`.
Не удаляйте lease вручную, пока не подтверждено отсутствие принадлежащих ему
процессов/устройств. Остановка не удаляет диагностические данные и базы dev-сервисов.

Типовые ошибки: неверный hostname; незагруженный image ID; занятый IP/GPU;
пересекающиеся CPU sets; неверная модель или каталог snapshot; недоступный peer.
Меняйте исходный deployment JSON и генерируйте новый комплект.

## Сетевая policy и ограничения

В serving-фазе backend работает от UID 65534 только в engine namespace
(loopback + WireGuard). Agent/helper работают от root внутри контейнера;
Cocoon services — от UID 10001. Root filesystem read-only, PID namespace private,
Docker socket не монтируется, доступна только выделенная GPU. Dev supervisor
получает capabilities для создания вложенных namespaces; это не production CVM.
JIT-кэш PyTorch/Triton находится в приватном каталоге epoch на `/run` с разрешённым
исполнением; установленные helpers остаются на read-only root filesystem.

Для `external-fake-ton` укажите в `services` числовые `proxy_ip`,
`proxy_worker_port`, `proxy_client_port`, `key_manager_ip`, `key_manager_port`
и `coefficient`. Вместо локального стека запускается только worker/router.
Default-drop разрешает UID 10001 исходящие TCP только к proxy worker port
и key-manager port. Другие адреса, DNS, скачивание моделей и общий Internet
egress во время обслуживания не открываются. При необходимости маршрутизации
за LAN задайте `gateway` соответствующему rank. Внешние fake-TON client/proxy/KM
должны быть подготовлены с той же моделью и публичными dev identities.
Настоящий TON и production attestation требуют отдельного production profile.

Шаг 13 (длительная нагрузка и benchmark) и отложенный шаг 5 (лимиты очередей)
этой упаковкой не заменяются.

## Проверка конфигурации без GPU

```bash
python3 -B test/test-pipeline-deployment.py
```

На выделенной Linux VM подготовьте simulator image и JSON с двумя локальными
rank, затем запустите lifecycle suite. `--service-ip` — третий свободный адрес
той же LAN для внешних dev proxy/client/key-manager:

```bash
sudo python3 -B test/test-pipeline-deployment-linux.py \
  --config deployment.json --artifact /opt/cocoon-image-simulator/artifact.json \
  --service-ip 192.168.100.14 --output /opt/cocoon-deployment-test
```

Для этого теста на хосте дополнительно нужен `nftables` (`sudo apt-get install -y
nftables`). Он сравнивает правила firewall хоста до и после теста.

Тест запускает зависимости, выполняет assertions и cleanup, сохраняет
`result.json` и возвращает ненулевой код при ошибке. Каталог результата должен
быть новым. Проверяются неверный artifact/endpoint, полный Cocoon-запрос,
изоляция, SIGKILL агента/supervisor, частичный старт, повторный запуск и stop,
внешние служебные соединения при default drop и запрещённые направления.

## Проверка на двух GPU-хостах

После подготовки образа и моделей заполните свой файл по образцу
`experiments/gpu-pipeline/lab.json`: SSH-адреса, ключ, hostname, LAN-адреса,
интерфейс и каталог моделей. Нужны passwordless sudo и свободная GPU на каждом
хосте. Тест использует дополнительные адреса `lan + 10`, CPU 0–7 и 24 GiB RAM
на rank; убедитесь, что они свободны. `--remote-dir` — отдельный каталог теста,
доступный для записи вашему SSH-пользователю,
`--artifact-dir` и `--image-archive` — пути на head.

```bash
python3 -B test/test-pipeline-deployment-gpu.py \
  --backend vllm --model large --lab my-lab.json \
  --remote-dir /home/user/cocoon-deployment-test \
  --artifact-dir /opt/cocoon-image-vllm \
  --image-archive /opt/cocoon-image-vllm/image.tar \
  --output build/deployment-vllm
```

Для SGLang используйте его образ и `--backend sglang`, новый output-каталог.
Тест переносит образ при необходимости, запускает оба комплекта, отправляет
полный Cocoon-запрос, проверяет изоляцию реальных backend/helper, SIGKILL агента,
освобождение GPU, повторное развёртывание и запрос после восстановления.
Cleanup выполняется и при ошибке; результат и диагностика сохраняются в output.
`--image-archive` можно опустить, если точный image ID уже загружен на обоих хостах.

Версии, конфигурации, результаты и ограничения приёмки:
[STEP12-REPORT.md](STEP12-REPORT.md).
