# Проверка двух QEMU/VFIO VM на GPU-хостах

Дата: 2026-10-05. Исходный checkout: `9e638b7`, плюс VM launcher и тесты
из текущего изменения. Статус: **пройдено: две настоящие VM, GPU inference и cleanup**.

Предыдущая приёмка шага 12 проверяла simulator в Linux VM и отдельно GPU
контейнеры на bare metal. В этом прогоне QEMU действительно запущен на каждом
физическом GPU-хосте; pipeline containers находятся внутри гостей. Это обычные
VM в явном dev-режиме, `hardware_attested=false`.

## Фактический стенд

| Компонент | Проверенная версия/параметр |
|---|---|
| Физические хосты | hetzner-cuda-01 / hetzner-cuda-02 |
| Host kernel | 7.0.0-34-generic / 7.0.0-22-generic |
| QEMU / libvirt | 10.2.1 / 12.0.0 |
| OVMF / virtiofsd | 2025.11-3ubuntu7.2 / 1.13.2-6ubuntu0.1 |
| Guest base | Ubuntu noble cloud image, build 20260926 |
| Guest kernel | 6.8.0-146-generic |
| Guest NVIDIA | 580.178.04, open kernel module |
| Guest Docker / toolkit | 29.1.3 / NVIDIA Container Toolkit 1.20.1 |
| VM resources | 8 vCPU, 32768 MiB RAM, 200 GiB sparse disk каждая |
| Model/backend | Qwen3-14B BF16, vLLM 0.29.0+cu129, PP=2, TP=1 |
| Rank resources | CPU 0–7, 24576 MiB RAM, одна RTX 4000 SFF Ada 20 GiB |
| Context/concurrency | 4096 / 1 |

Host GPU `0000:01:00.0` и audio `.1` находятся вместе в IOMMU group 14,
назначаются одной VM и во время её работы используют `vfio-pci`. В гостях GPU
имеет BDF `0000:09:00.0`, UUID совпадают с физическими картами:

- Head: `GPU-83a5e615-f17d-1786-3685-23c663eb5dc5`.
- Member: `GPU-a0aea187-6c11-dba2-5bc1-557a6caf9fc3`.

`systemd-detect-virt` в обоих гостях возвращает `kvm`; libvirt domain XML
содержит оба managed PCI hostdev и QEMU type `kvm`. Состояние, domain XML,
guest GPU/OS/packages сохранены в `build/vfio-vm/inventory-{0,1}.json`.

## Artifacts

```text
Ubuntu image SHA-256:
6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2
Runtime image ID:
sha256:e8a829e200d1d5fb38d798807cebd3fb4f45cd8d08aac622e7511ead9862e9e3
Transferred image.tar SHA-256 on both hosts:
c50178606758c86ed08ccade011f1dea7f2047f3deae149a5269622b0f3ad8be
Deployment ID:
00d8db46b56c096a63bb23b7ac701ef8a064918faa1d4805a6c28ff0af278336
```

Подпись Ubuntu SHA256SUMS проверена `gpgv` ключом из
`ubuntu-cloudimage-keyring`. Одинаковый runtime archive загружен в отдельный
Docker store каждого гостя. Model/artifact shares смонтированы через read-only
virtiofs. В runtime source checkout и host Docker socket не передаются.

## Проверки

- Обе VM загрузились; видны реальные GPU и NVIDIA driver внутри гостей.
- До inference выполнен полный stop/start обеих VM: GPU возвращались к
  `nvidia`/`snd_hda_intel`, затем снова передавались `vfio-pci` и работали в guest.
- Межгостевой ping через LAN с payload 1372, `-M do`, MTU 1400: 3/3, без потерь.
- Полный client → proxy → один worker → gate → vLLM PP=2 запрос вернул ответ
  и ненулевой usage. SSE завершился `[DONE]`; `/v1/models` показал одного worker.
- Пройдены runtime/model read-only mounts, private PID, GPU UUID assignment,
  реальные backend/helper UID/capabilities/netns/cgroup и отсутствие underlay FD.
- Повторный start не пересоздал контейнеры.
- SIGKILL head-agent вызвал внешний cleanup; GPU-память освобождена.
- Новый epoch после сбоя и повторный inference прошли. Итог GPU suite: `ok=true`,
  7 проверок, cleanup confirmed на обоих ranks, GPU memory `[2, 2]` MiB.
- Заключительная остановка VM прошла: обе `shut off`, GPU снова на `nvidia`,
  audio на `snd_hda_intel`, ownership `released=true`, persistence восстановлен.
  Финальная проверка хостов: NVIDIA 595.91.07, по 2 MiB, compute processes нет,
  существующий `open-webui` на head healthy. Доказательства —
  `build/vfio-vm/stop-{0,1}.json` и `final-host-check.json`.

JSON-ответ содержит 43 tokens usage (11 prompt + 32 completion). В коротком smoke
задан предел 32 токена: Qwen вернула начало thinking-текста с `finish_reason=length`.
Это проверка транспорта/инференса, а не качества законченного ответа. SSE вернул
34 chunks и `[DONE]`. Для презентационного запроса подбирайте prompt и бюджет
генерации отдельно.

Снимок после загрузки модели: head **17754 MiB**, member **17838 MiB**
(примерно 17.3–17.4 GiB). Счётчики WireGuard передавали данные на обоих ranks;
снимки находятся в `build/vfio-vm/live-gpu-network-{0,1}.json`. Это измерение
одного момента, не утверждение о пике памяти или benchmark.

Локально прошли 10 тестов VM lifecycle contracts, 19 deployment tests, проверка
синтаксиса shell-скриптов. Пример `example-vm.json` после нормализации совпадает
с реально использованной конфигурацией.

Команда GPU-приёмки:

```bash
python3 -B test/test-pipeline-vm-gpu.py \
  --backend vllm --model large \
  --remote-dir /home/cocoon/cocoon-vm-test \
  --artifact-dir /srv/cocoon-artifacts/vllm \
  --output build/vfio-vm/acceptance-vllm
```

Результаты и диагностика — `build/vfio-vm/acceptance-vllm/`, журнал —
`build/vfio-vm/acceptance-vllm.log`. Private guest SSH key лежит отдельно в
`build/vfio-vm/id_ed25519`; весь каталог build публиковать нельзя.

## Неуспешные промежуточные попытки

1. libvirt отклонил MTU непосредственно на direct interface. Убрано unsupported
   XML-поле; MTU задаётся в госте и проверяется относительно хостовой LAN.
2. Firmware autodetection выбрала AMD SEV OVMF на Intel host; AppArmor отказал
   запуску. Выбрана обычная OVMF явно, AppArmor гипервизора не отключался.
3. `nvtop` держал NVIDIA FD при отсутствии compute jobs, из-за чего PCI unbind
   ожидал освобождения GPU. Подтверждённые процессы `nvtop` остановлены. Launcher
   теперь проверяет device users после остановки persistence daemon и до detach;
   timeout сохраняет неопределённое владение, не выдаёт ложный успешный cleanup.

## Границы

Успех этого прогона относится к **vLLM на двух обычных QEMU/VFIO VM**.
SGLang ранее проверялся на bare metal; его VM-квалификация отдельно не выполнена.
CC, TDX/GPU attestation, measured guest image, production TON, произвольное N,
длительная нагрузка и аварии гипервизора не покрываются. Два managed hostdev и
шифрование WireGuard не делают обычную VM confidential.

Гостевые APT-пакеты не полностью закреплены; версии сохранены, но это не
воспроизводимый measured production image. Дополнительные работы и процедура
повторного demo описаны в [VM-DEPLOYMENT.md](VM-DEPLOYMENT.md), а production trust
остаётся открытой задачей P7-01/P7-03/P7-04 из плана.
