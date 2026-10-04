# Шаг 2: результаты GPU-эксперимента

Дата: **2026-10-04, Europe/Istanbul**. Идентификаторы запусков используют UTC.

**Оба backend прошли итоговую матрицу:** Qwen3-0.6B в PP=1 и PP=2, затем
Qwen3-14B BF16 в PP=2 через WireGuard. TP=1, context limit 4096, один активный
запрос, CPU weight offload выключен. Выполнены шесть GPU-запусков, два сравнения
PP=1/PP=2 и девять локальных тестов. Это результат dev-стенда без CC.

Команды воспроизведения — в [README.md](README.md). Закреплённые образы и модели —
в [profiles.json](profiles.json), адреса и MTU — в [lab.json](lab.json).
Полные числовые результаты, effective argv/env, фрагменты логов и SHA-256 исходников
и артефактов сохранены в [verification.json](evidence/verification.json).

## Оборудование и версии

Два хоста `hetzner-cuda-01` / `hetzner-cuda-02`: по RTX 4000 SFF Ada с **20475 MiB**
памяти, NVIDIA driver **595.91.07**, Intel Core i5-13500 (20 logical CPUs), около
64 GB RAM. LAN — `192.168.100.2/3`, VLAN MTU 1400; WG — `10.231.0.1/2`, MTU 1320.
Версии ОС, Docker, WireGuard tools и iperf: [hardware.json](evidence/hardware.json).

| Backend | Версия | PyTorch | CUDA runtime | NCCL runtime (логи) |
|---|---|---|---|---|
| SGLang | 0.5.10.post1 | 2.9.1+cu130 | 13.0 | 2.28.3 |
| vLLM | 0.29.0+cu129 | 2.13.0+cu129 | 12.9 | 2.30.7 |

`torch.cuda.nccl.version()` в этих образах сообщает 2.27.7 и 2.29.7, но
фактически загруженная библиотека в NCCL INIT-логах имеет версии **2.28.3** и
**2.30.7** соответственно — как в синтетическом тесте, так и в backend.
В evidence сохранены оба значения; runtime-версия определяется по логам.

Image digests закреплены целиком. Qwen3-0.6B использует revision
`c1899de289a04d12100db370d81485cdf75e47ca`; Qwen3-14B —
`40c069824f4251a91eefaf281ebe4c544efd3e18`. SHA-256 всех файлов совпали между
хостами: [small manifest](evidence/model-small-manifest.json),
[large manifest](evidence/model-large-manifest.json). Во время inference файлы
смонтированы read-only, доступ к Hugging Face отключён.

## Корректность и capacity

В каждом запуске прошли warmup, JSON, incremental SSE, finish reason и usage,
EOS/stop, закрытие клиентского потока с последующим успешным запросом и длинный
prefill. Длинный запрос содержал **2070 prompt tokens**, ответ — 2 токена.

На малой модели для каждого backend совпали тексты трёх greedy fixtures, токены,
usage и причины завершения между PP=1 и PP=2. Максимальное абсолютное отличие
logprobs — **0.0** для обоих backend при заданном допуске 0.15. Эти fixtures
проверяют согласованность топологий: малая модель, например, одинаково ошиблась
на арифметическом вопросе в обоих режимах. Это не оценка качества модели.

Safetensors headers Qwen3-14B содержат **14 768 307 200 BF16 параметров**:
**29 536 614 400 bytes = 27.51 GiB** весов. Это больше 19.995 GiB одной GPU ещё
до KV cache и workspace. PP=1 для 14B не запускался: нехватка памяти следует из
фактического размера весов. В PP=2 оба backend загрузили примерно половину весов
на каждый rank и обслужили запросы без CPU offload. Участие обеих GPU подтверждают
логи rank/загрузки весов, GPU utilization, обмен через WG и остановка генерации
при уничтожении процессов rank 1.

| Backend / модель / PP | Максимум GPU-памяти rank 0 / rank 1, MiB | TTFT SSE, ms | Inter-chunk p50, ms |
|---|---:|---:|---:|
| SGLang / 0.6B / 1 | 2118 / — | 11.12 | 7.81 |
| SGLang / 0.6B / 2 | 1480 / 1502 | 45.26 | 37.49 |
| SGLang / 14B / 2 | 15204 / 15274 | 127.49 | 107.98 |
| vLLM / 0.6B / 1 | 18172 / — | 39.42 | 7.46 |
| vLLM / 0.6B / 2 | 18332 / 18356 | 52.53 | 6.10 |
| vLLM / 14B / 2 | 18106 / 18258 | 227.44 | 107.78 |

Память измерялась `nvidia-smi` каждые 200 ms; таблица показывает наблюдавшийся
максимум всего запуска. vLLM резервирует KV cache по memory fraction 0.9;
SGLang ограничен `max_total_tokens=4096`, поэтому общие reservations различаются.
Для 14B длинный запрос занял 1.685 s на SGLang и 1.758 s на vLLM. Запрос после
отмены завершился за 2.505 s и 0.492 s соответственно.

Это одиночные функциональные замеры после warmup, с отключёнными CUDA graphs;
они не заменяют benchmark-матрицу шага 13. SSE на 14B содержал 30 generated
tokens в 29 content chunks: inter-chunk latency не тождественна inter-token
latency. По этому запуску нельзя обещать ускорение от PP или переносить цифры
на confidential deployment.

## Сеть и фактический транспорт

У каждого rank проверены только интерфейсы `lo` и `wg0`, отсутствие default route,
Docker `--network none`, отсутствие published ports, `NET_ADMIN` и Docker socket.
NCCL логи обоих backend показывают `NET/Socket` и адреса `wg0:10.231.0.1/2`;
IB, P2P и SHM отключены. API доступен по loopback внутри head namespace.

| Серия | LAN RTT avg, ms | WG RTT avg, ms | LAN throughput, Mbit/s | WG throughput, Mbit/s | Head CPU busy LAN / WG |
|---|---:|---:|---:|---:|---:|
| SGLang | 0.805 | 1.585 | 934.41 | 889.70 | 2.23% / 5.02% |
| vLLM | 0.811 | 1.666 | 934.44 | 889.29 | 3.97% / 5.27% |

Ping: 8 пакетов с payload 1292 bytes и DF; iperf: один поток, 5 секунд.
CPU — разность `/proc/stat` на head, доля суммарного времени 20 logical CPUs,
включая фоновые процессы. Ранний замер во время скачивания образов исключён
из этой таблицы. Это короткие измерения, не изолированный CPU benchmark.

| NCCL ping-pong | SGLang round-trip mean, ms | vLLM round-trip mean, ms |
|---|---:|---:|
| 16 KiB, 30 повторов | 2.003 | 1.951 |
| 64 KiB, 30 повторов | 1.648 | 1.723 |
| 64 MiB, 4 повтора | 1207.19 | 1207.48 |

Корректность CUDA-буферов проверена на обоих rank. Средние включают первый
обмен; для 64 MiB полезная пропускная способность составила около 889 Mbit/s.
В каждой PP=2 серии сохранено до 50 000 underlay packets на хост: обнаружен WG UDP,
открытого TCP или другого UDP в выборке нет. Capture дополняет проверку namespaces
и NCCL, а не доказывает конфиденциальность самостоятельно. Счётчики WG до и после
API-проверок также сохранены.

## Отказы и ограничения закреплённых версий

В активном SSE-запросе после появления контента уничтожались backend-процессы
rank 1. Его namespace сохранялся до cleanup, чтобы отделить смерть процесса
от исчезновения интерфейса. Ложное успешное завершение запрещено тестом;
клиентский timeout сам по себе не засчитывается.

| Backend | 0.6B: длительность fault-запроса | 14B: длительность fault-запроса | Результат |
|---|---:|---:|---|
| SGLang | 2.879 s | 6.267 s | Неполный SSE EOF без finish reason, `[DONE]` и итогового usage |
| vLLM | 32.987 s | 32.551 s | SSE error `InternalServerError`, code 500; нет finish reason и итогового usage |

Время отсчитывается от начала запроса, включая генерацию до инъекции отказа.
vLLM посылает `[DONE]` **после error event**; это завершение ошибочного потока,
не успешный ответ. HTTP headers к моменту отказа уже отправлены.

Обнаружены и сохранены [два неуспешных профиля](evidence/failed-configurations.json):

1. **SGLang с chunked prefill:** PP=2 упал на длинном запросе с
   `token_to_kv_pool_allocator memory leak detected`, оставив 2048 из 4096 KV
   slots занятыми. Итоговый профиль явно задаёт `--chunked-prefill-size -1` в
   PP=1 и PP=2. Все проверки с ним повторены. Chunked prefill этой версии для
   данного PP-профиля не квалифицирован.
2. **vLLM с default RPC deadline:** worker обнаруживал смерть rank, но HTTP/SSE
   ждал ответ RPC и клиент истекал по timeout. Итоговый профиль задаёт
   `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30`, `TORCH_NCCL_ASYNC_ERROR_HANDLING=1`
   и `TORCH_NCCL_WAIT_TIMEOUT_DUMP_MILSEC=1000`. Повторные отказы завершились
   явной API-ошибкой. RPC deadline применяется самим backend;
   [закреплённый executor](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/executor/multiproc_executor.py)
   использует его при ожидании `execute_model`/`sample_tokens`.

Эти параметры входят в воспроизводимый dev-профиль. Полноценные deadlines,
supervisor, gate и перенос ошибок в существующий Cocoon worker относятся к
следующим шагам плана; этот эксперимент их не реализует.

## Артефакты и cleanup

| Серия | Run ID (UTC) |
|---|---|
| SGLang 0.6B PP=1 | `20261003234539-sglang-small-pp1-ec6d6c` |
| SGLang 0.6B PP=2 | `20261003234634-sglang-small-pp2-26a2c4` |
| SGLang 14B PP=2 | `20261003234858-sglang-large-pp2-f0bcf3` |
| vLLM 0.6B PP=1 | `20261003235113-vllm-small-pp1-b3758b` |
| vLLM 0.6B PP=2 | `20261003235222-vllm-small-pp2-0bba24` |
| vLLM 14B PP=2 | `20261003235510-vllm-large-pp2-76186f` |

Полные файлы лежат в `experiments/gpu-pipeline/results/<run>/` локально и
`/home/ruslixag/cocoon-pipeline-dev/runs/<run>/` на хостах. Объёмные локальные
артефакты исключены из Git; компактные доказательства и hashes находятся в
`evidence/`. SHA-256 исходников всех шести итоговых запусков совпадают с
текущими скриптами.

[Финальная проверка cleanup](evidence/cleanup.json) подтвердила на обоих хостах:
нет тестовых контейнеров, GPU-процессов, monitor-процессов, WG keys/interfaces
и слушателя UDP 51888; GPU вернулись к baseline 2 MiB. Проверено также завершение
контроллера через SIGINT с очисткой обоих rank. Существующие `vllm-head` и
`vllm-worker` сохранены и оставлены остановленными согласно разрешению пользователя.
Модели, образы и логи сохранены для повторного запуска.

**Вывод:** вычислительная основа шага 2 подтверждена для обоих указанных профилей.
CC/TDX/GPU attestation, private RAM, рабочий Cocoon gate и billing не проверялись;
они остаются отдельными этапами исходного плана.
