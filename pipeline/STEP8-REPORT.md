# Проверка шага 8 — 2026-10-04

Реализован и проверен dev-профиль `simulator-dev-pp2-wg-v1`: два агента, mutual TLS, настоящий WireGuard, firewall, непривилегированный backend в отдельной сети, обнаружение отказов и cleanup перед новым epoch. Это проверка Linux networking с синтетическим трафиком; GPU pipeline, CVM и аппаратная конфиденциальность не заявляются.

## Среда и воспроизведение

- Mac ARM64; отдельная Ubuntu 24.04 VM `cocoon-pipeline-net`, 4 vCPU / 6 GiB RAM.
- Kernel `7.0.11-orbstack-00360-gc9bc4d96ac70`, aarch64, glibc 2.39.
- iproute2 6.1.0, wireguard-tools 1.0.20210914, nftables 1.0.9.
- CMake Release / clang, VM build `/opt/cocoon-build`, checkout `/work/cocoon`.
- Два выделенных underlay namespaces внутри одной VM; каждый агент создаёт собственный engine namespace.

Команда с Mac после [подготовки VM](README.md#wireguard-и-linux-vm--шаг-8):

```bash
orbctl run -m cocoon-pipeline-net -u root python3 \
  /work/cocoon/test/test-pipeline-network.py --build-dir /opt/cocoon-build
```

Итоговый запуск: `/tmp/cpn-k89jyh_5/report.json`, **7/7, PASS**, 114.37 s. Перед интеграционными сценариями прошли C++ profiles, membership/network protocol и настоящие mutual TLS handshakes с негативными policy cases.

## Результаты

| Сценарий | Подтверждённый результат |
|---|---|
| TCP echo и изоляция | По 32 KiB из каждой engine namespace в другую; ответ совпадает. Только `lo`/`wg0`, peer `/32`, нет default/underlay route. UID/GID backend 65534, capabilities обнулены, `NoNewPrivs=1`. |
| Firewall | С underlay недоступны внутренние порты. Специальный underlay listener доступен локально, но соединение roster peer к нему истекает по timeout. Непривилегированный процесс не может добавить маршрут или войти в init network namespace. |
| Потеря data | Группа теряет READY; после восстановления — новый epoch и network key, старые backend/namespace удалены. |
| Потеря control | Группа закрывается и восстанавливается только новым epoch. |
| Удаление `wg0` | В удерживаемой backend namespace остаётся только loopback; до cleanup уже невозможно отправить данные ни peer, ни underlay/Internet. После возобновления supervisor группа пересоздаётся с новым ключом. |
| Изменение firewall | Helper обнаруживает изменение своей таблицы, группа закрывается, старая сеть удаляется перед restart. |
| SIGKILL helper | Агент подтверждает остановку старого helper, recovery без private key удаляет его сеть и запись владения; новая группа работает. |
| Ошибка настройки | При занятом 51820/UDP настройка падает после создания части сети. Ни один backend не запускается; две повторные попытки ограничены, все свои ресурсы удалены, посторонний процесс остаётся жив. |

Первый тест объединяет TCP echo, firewall и privileges. Capture во время READY содержит **150 WireGuard UDP packets и 3 control TCP packets**, посторонних IPv4 packets — **0**. Случайный plaintext marker, переданный через echo, в pcap отсутствует. Из захвата исключены bridge-origin packets тестового стенда; захватывается весь трафик участников после READY. Это дополняет проверки конфигурации и прав, а не заменяет их.

После итогового запуска: пустой список namespaces, пустой каталог owner records, нет процессов agent/helper/backend; неизменность root firewall проверена. `Node.clean()` проверяет удаление ресурсов **до** аварийного cleanup самого тестового стенда.

На Mac дополнительно прошли 13 групповых и 16 локальных интеграционных сценариев; C++ profile/network protocol tests; обычный JSON/SSE single-worker smoke Cocoon. Нативные тесты запускались с разрешёнными локальными sockets: sandbox без этого разрешения отклонял `bind`, что не является результатом проверки функциональности.

Артефакты этой сессии скопированы в локальный игнорируемый каталог `build/step8-evidence-20261004/`: `linux/`, `mac-group/`, `mac-supervisor/`, `mac-smoke/`. JSON-отчёты содержат hashes исходников; Linux-каталог содержит pcap, независимые network evidence и логи всех сценариев. При новом запуске создаются новые каталоги; результаты не перезаписываются.

## За рамками проверки

Нет worker gate/реальных Cocoon-запросов через эту сеть, SGLang/vLLM, CUDA/NCCL, контейнерного GPU deployment или настоящей аттестации. Нет доказательства cleanup всего дерева после SIGKILL самого агента/одновременной потери агента и helper. Service egress полного worker, users/groups/mount/device policy и упаковка helpers в закреплённый image ещё требуют детализации. Эти работы и условия возврата записаны в [плане: остаток шага 8](../pipeline-plan.md#за-скобками-реализации-шага-8), включая сохраняющийся P7-02 и новые P8-01/P8-02.
