#!/usr/bin/env bash
#
# create_relay.sh — Создание нового relay-сервера в Yandex Cloud
#
# Что делает:
#   1. Резервирует статический публичный IP
#   2. Создаёт VM (2 vCPU 20%, 1GB RAM, 10GB HDD, Ubuntu 22.04)
#   3. Ждёт запуска VM
#   4. Устанавливает nginx + stream модуль
#   5. Настраивает SNI-based relay на все 3 сервера
#   6. Добавляет relay host'ы в Marzban (5 ОБХОД'ов с российскими SNI)
#   7. Выводит итоговую информацию
#
# Использование:
#   ./create_relay.sh [--name relay-2] [--dry-run]
#
# Требования:
#   - yc CLI установлен и настроен (yc init)
#   - SSH ключ ~/.ssh/yc_relay (создаётся автоматически если нет)
#   - curl, python3, sshpass (опционально)
#
# ─── Конфигурация ────────────────────────────────────────────────

set -euo pipefail

# Yandex Cloud
YC_BIN="${YC_BIN:-$HOME/yandex-cloud/bin/yc}"
YC_ZONE="ru-central1-a"
YC_SUBNET=""  # auto-detect after args parsing
YC_PLATFORM="standard-v3"
YC_CORES=2
YC_CORE_FRACTION=20
YC_MEMORY=1          # GB
YC_DISK_SIZE=10      # GB
YC_DISK_TYPE="network-hdd"
YC_IMAGE_FAMILY="ubuntu-2204-lts"
YC_IMAGE_FOLDER="standard-images"

# SSH
SSH_KEY="$HOME/.ssh/yc_relay"
SSH_USER="yc-user"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15"

# Серверы (куда relay пробрасывает трафик)
ESTONIA_IP="45.92.174.214"
NETHERLANDS_IP="72.56.71.124"
GERMANY_IP="81.200.156.43"

# Marzban API
MARZBAN_API="https://vpn.psysoldatov.ru:8443"
MARZBAN_USER="xlist"
MARZBAN_PASS="Kereekin95"

# SNI → Сервер маппинг для relay
# Формат: SNI|TARGET_IP|INBOUND_TAG|ОБХОД_NAME
RELAY_ROUTES=(
    "ads.x5.ru|${ESTONIA_IP}|VLESS_REALITY_X5|ОБХОД {N}-1"
    "api-maps.yandex.ru|${ESTONIA_IP}|VLESS_REALITY_YANDEX|ОБХОД {N}-2"
    "eh1.vk.com|${NETHERLANDS_IP}|VLESS_REALITY_VK|ОБХОД {N}-3"
    "smartcaptcha.yandexcloud.net|${NETHERLANDS_IP}|VLESS_REALITY_CAPTCHA|ОБХОД {N}-4"
    "io.ozone.ru|${GERMANY_IP}|VLESS_REALITY_OZONE|ОБХОД {N}-5"
)

# Дополнительные SNI (не для ОБХОД'ов, но роутятся через relay)
EXTRA_ROUTES=(
    "www.ietf.org|${NETHERLANDS_IP}"
    "ietf.org|${NETHERLANDS_IP}"
    "www.microsoft.com|${ESTONIA_IP}"
    "microsoft.com|${ESTONIA_IP}"
)

# ─── Параметры ───────────────────────────────────────────────────

VM_NAME="relay-server"
DRY_RUN=false
RELAY_NUMBER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)    VM_NAME="$2"; shift 2 ;;
        --number)  RELAY_NUMBER="$2"; shift 2 ;;
        --zone)    YC_ZONE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help)
            echo "Использование: $0 [--name relay-2] [--number 2] [--zone ru-central1-b] [--dry-run]"
            echo ""
            echo "  --name    Имя VM в Yandex Cloud (default: relay-server)"
            echo "  --number  Номер relay для именования ОБХОД'ов (auto-detect если не указан)"
            echo "  --zone    Зона Yandex Cloud (default: ru-central1-a)"
            echo "            Доступные зоны:"
            echo "              ru-central1-a  (Москва, подсеть 158.160.x.x)"
            echo "              ru-central1-b  (Москва, другой блок IP)"
            echo "              ru-central1-d  (Москва, другой блок IP)"
            echo "  --dry-run Показать что будет сделано, не выполняя"
            exit 0
            ;;
        *) echo "Неизвестный параметр: $1"; exit 1 ;;
    esac
done

# Авто-определение subnet по зоне
YC_SUBNET="${YC_SUBNET:-default-${YC_ZONE}}"

# ─── Функции ─────────────────────────────────────────────────────

log()  { echo "$(date '+%H:%M:%S') [INFO]  $*"; }
warn() { echo "$(date '+%H:%M:%S') [WARN]  $*" >&2; }
err()  { echo "$(date '+%H:%M:%S') [ERROR] $*" >&2; exit 1; }

check_deps() {
    [[ -x "$YC_BIN" ]] || err "yc CLI не найден: $YC_BIN. Установите: curl -sSL https://storage.yandexcloud.net/yandexcloud-yc/install.sh | bash"
    command -v curl    >/dev/null || err "curl не установлен"
    command -v python3 >/dev/null || err "python3 не установлен"
    command -v ssh     >/dev/null || err "ssh не установлен"
}

ensure_ssh_key() {
    if [[ ! -f "$SSH_KEY" ]]; then
        log "Генерация SSH ключа: $SSH_KEY"
        ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "yc-relay" >/dev/null 2>&1
    fi
    log "SSH ключ: $SSH_KEY"
}

auto_detect_relay_number() {
    if [[ -n "$RELAY_NUMBER" ]]; then
        return
    fi
    # Считаем существующие relay VM по имени
    local count
    count=$("$YC_BIN" compute instance list --format json 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for i in d if 'relay' in i.get('name','')))" 2>/dev/null || echo "0")
    RELAY_NUMBER=$((count + 1))
    log "Автоопределён номер relay: $RELAY_NUMBER"
}

reserve_static_ip() {
    local ip_name="${VM_NAME}-ip"
    log "Резервирование статического IP: $ip_name"

    if $DRY_RUN; then
        log "[DRY-RUN] yc vpc address create --name $ip_name --external-ipv4 zone=$YC_ZONE"
        RELAY_IP="<dry-run-ip>"
        return
    fi

    local result
    result=$("$YC_BIN" vpc address create \
        --name "$ip_name" \
        --external-ipv4 zone="$YC_ZONE" \
        --format json 2>/dev/null)

    RELAY_IP=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin)['external_ipv4_address']['address'])")
    log "Статический IP зарезервирован: $RELAY_IP"
}

create_vm() {
    log "Создание VM: $VM_NAME (${YC_CORES} vCPU ${YC_CORE_FRACTION}%, ${YC_MEMORY}GB RAM, ${YC_DISK_SIZE}GB HDD)"

    if $DRY_RUN; then
        log "[DRY-RUN] yc compute instance create --name $VM_NAME ..."
        return
    fi

    "$YC_BIN" compute instance create \
        --name "$VM_NAME" \
        --hostname "$VM_NAME" \
        --zone "$YC_ZONE" \
        --network-interface "subnet-name=$YC_SUBNET,nat-ip-version=ipv4,nat-address=$RELAY_IP" \
        --platform-id "$YC_PLATFORM" \
        --cores "$YC_CORES" \
        --core-fraction "$YC_CORE_FRACTION" \
        --memory "$YC_MEMORY" \
        --create-boot-disk "image-folder-id=$YC_IMAGE_FOLDER,image-family=$YC_IMAGE_FAMILY,size=$YC_DISK_SIZE,type=$YC_DISK_TYPE" \
        --ssh-key "${SSH_KEY}.pub" \
        --async >/dev/null 2>&1

    log "VM создаётся, ожидание запуска..."
    wait_vm_running
}

wait_vm_running() {
    local attempts=0
    local max_attempts=20

    while [[ $attempts -lt $max_attempts ]]; do
        sleep 15
        local status
        status=$("$YC_BIN" compute instance get "$VM_NAME" --format json 2>/dev/null \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "UNKNOWN")

        if [[ "$status" == "RUNNING" ]]; then
            log "VM запущена!"
            return
        fi
        attempts=$((attempts + 1))
        log "Статус: $status (попытка $attempts/$max_attempts)"
    done

    err "VM не запустилась за $((max_attempts * 15)) секунд"
}

wait_ssh_ready() {
    log "Ожидание SSH..."
    local attempts=0
    while [[ $attempts -lt 12 ]]; do
        if ssh $SSH_OPTS -i "$SSH_KEY" "$SSH_USER@$RELAY_IP" "echo ok" >/dev/null 2>&1; then
            log "SSH доступен"
            return
        fi
        sleep 10
        attempts=$((attempts + 1))
    done
    err "SSH недоступен после 2 минут"
}

run_remote() {
    ssh $SSH_OPTS -i "$SSH_KEY" "$SSH_USER@$RELAY_IP" "$@"
}

install_nginx() {
    log "Установка nginx + stream модуль..."

    if $DRY_RUN; then
        log "[DRY-RUN] apt-get install nginx libnginx-mod-stream"
        return
    fi

    run_remote "sudo apt-get update -qq && sudo apt-get install -y -qq nginx libnginx-mod-stream" >/dev/null 2>&1
    log "nginx установлен"

    # Настройка worker_connections для relay нагрузки
    run_remote "sudo sed -i 's/worker_connections [0-9]*;/worker_connections 4096;\n\tmulti_accept on;/' /etc/nginx/nginx.conf"
    log "nginx worker_connections=4096, multi_accept=on"
}

configure_nginx_relay() {
    log "Настройка nginx stream relay..."

    # Генерируем конфиг
    local relay_conf=""
    relay_conf+="map \$ssl_preread_server_name \$relay_backend {\n"

    for route in "${RELAY_ROUTES[@]}"; do
        IFS='|' read -r sni target_ip _ _ <<< "$route"
        relay_conf+="    ${sni}    ${target_ip}:443;\n"
    done

    for route in "${EXTRA_ROUTES[@]}"; do
        IFS='|' read -r sni target_ip <<< "$route"
        relay_conf+="    ${sni}    ${target_ip}:443;\n"
    done

    relay_conf+="    default    ${ESTONIA_IP}:443;\n"
    relay_conf+="}\n\n"
    relay_conf+="server {\n"
    relay_conf+="    listen 443;\n"
    relay_conf+="    listen [::]:443;\n"
    relay_conf+="    ssl_preread on;\n"
    relay_conf+="    proxy_pass \$relay_backend;\n"
    relay_conf+="    proxy_connect_timeout 5s;\n"
    relay_conf+="    proxy_timeout 300s;\n"
    relay_conf+="}\n"

    if $DRY_RUN; then
        log "[DRY-RUN] Конфиг relay:"
        echo -e "$relay_conf"
        return
    fi

    # Создаём директорию и записываем конфиг
    run_remote "sudo mkdir -p /etc/nginx/stream.d"
    echo -e "$relay_conf" | run_remote "sudo tee /etc/nginx/stream.d/relay.conf" >/dev/null

    # Добавляем stream блок в nginx.conf если его нет
    run_remote "grep -q '^stream {' /etc/nginx/nginx.conf 2>/dev/null || echo -e '\nstream {\n    include /etc/nginx/stream.d/*.conf;\n}' | sudo tee -a /etc/nginx/nginx.conf >/dev/null"

    # Проверяем и перезапускаем
    run_remote "sudo nginx -t 2>&1" || err "nginx конфиг невалидный!"
    run_remote "sudo systemctl restart nginx"
    log "nginx relay настроен и запущен"
}

verify_relay() {
    log "Проверка relay..."

    if $DRY_RUN; then
        log "[DRY-RUN] openssl s_client -connect $RELAY_IP:443 -servername ads.x5.ru"
        return
    fi

    # Проверяем TLS handshake через relay
    local result
    result=$(echo | openssl s_client -connect "$RELAY_IP:443" -servername ads.x5.ru -brief 2>&1 | head -3)

    if echo "$result" | grep -q "CONNECTION ESTABLISHED"; then
        log "Relay работает! TLS handshake через $RELAY_IP → Estonia OK"
    else
        warn "TLS handshake не прошёл. Проверьте вручную."
        echo "$result"
    fi
}

get_marzban_token() {
    log "Авторизация в Marzban API..."

    if $DRY_RUN; then
        MARZBAN_TOKEN="<dry-run-token>"
        return
    fi

    # Получаем токен (запрос через relay сервер, т.к. напрямую может не работать)
    local response
    response=$(run_remote "curl -sk -X POST '${MARZBAN_API}/api/admin/token' \
        -H 'Content-Type: application/x-www-form-urlencoded' \
        -d 'username=${MARZBAN_USER}&password=${MARZBAN_PASS}'" 2>/dev/null)

    MARZBAN_TOKEN=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null) \
        || err "Не удалось авторизоваться в Marzban. Ответ: $response"

    log "Marzban токен получен"
}

add_marzban_hosts() {
    log "Добавление relay host'ов в Marzban..."

    if $DRY_RUN; then
        for route in "${RELAY_ROUTES[@]}"; do
            IFS='|' read -r sni _ inbound_tag obhod_name <<< "$route"
            obhod_name="${obhod_name//\{N\}/$RELAY_NUMBER}"
            log "[DRY-RUN] Добавить в $inbound_tag: 🛡 $obhod_name ($sni → $RELAY_IP)"
        done
        return
    fi

    # Получаем текущие host'ы
    local hosts_json
    hosts_json=$(run_remote "curl -sk '${MARZBAN_API}/api/hosts' \
        -H 'Authorization: Bearer ${MARZBAN_TOKEN}'" 2>/dev/null)

    # Добавляем relay host'ы через Python
    local routes_json="["
    for route in "${RELAY_ROUTES[@]}"; do
        IFS='|' read -r sni target_ip inbound_tag obhod_name <<< "$route"
        obhod_name="${obhod_name//\{N\}/$RELAY_NUMBER}"
        routes_json+="{\"sni\":\"$sni\",\"tag\":\"$inbound_tag\",\"name\":\"$obhod_name\"},"
    done
    routes_json="${routes_json%,}]"

    local new_hosts
    new_hosts=$(python3 -c "
import json, sys

hosts = json.loads('''$hosts_json''')
relay_ip = '$RELAY_IP'
routes = json.loads('''$routes_json''')

for route in routes:
    tag = route['tag']
    if tag not in hosts:
        print(f'WARNING: inbound {tag} не найден в Marzban', file=sys.stderr)
        continue

    # Проверяем что такой relay ещё не добавлен
    exists = any(h['address'] == relay_ip and h['sni'] == route['sni'] for h in hosts[tag])
    if exists:
        print(f'SKIP: {route[\"name\"]} уже существует в {tag}', file=sys.stderr)
        continue

    hosts[tag].append({
        'remark': '🛡 ' + route['name'],
        'address': relay_ip,
        'port': 443,
        'sni': route['sni'],
        'host': '', 'path': '',
        'security': 'inbound_default',
        'alpn': '', 'fingerprint': 'random',
        'allowinsecure': False, 'is_disabled': False,
        'mux_enable': False, 'fragment_setting': None,
        'noise_setting': None, 'random_user_agent': False,
        'use_sni_as_host': False
    })
    print(f'ADD: {route[\"name\"]} → {tag} ({route[\"sni\"]})', file=sys.stderr)

print(json.dumps(hosts))
" 2>&1)

    # Отделяем JSON от логов
    local json_part
    json_part=$(echo "$new_hosts" | grep -v -E '^(ADD|SKIP|WARNING):')
    local log_part
    log_part=$(echo "$new_hosts" | grep -E '^(ADD|SKIP|WARNING):' || true)

    echo "$log_part" | while read -r line; do
        [[ -n "$line" ]] && log "  $line"
    done

    # Записываем на relay сервер и отправляем в Marzban
    echo "$json_part" | run_remote "cat > /tmp/new_hosts.json"

    local result
    result=$(run_remote "curl -sk -X PUT '${MARZBAN_API}/api/hosts' \
        -H 'Authorization: Bearer ${MARZBAN_TOKEN}' \
        -H 'Content-Type: application/json' \
        -d @/tmp/new_hosts.json" 2>/dev/null)

    # Проверяем результат
    python3 -c "
import json, sys
d = json.loads('''$result''')
for tag, hosts in d.items():
    relay_count = sum(1 for h in hosts if h['address'] == '$RELAY_IP')
    if relay_count > 0:
        print(f'  {tag}: {len(hosts)} hosts ({relay_count} relay)')
" 2>/dev/null

    log "Host'ы добавлены в Marzban"
}

print_summary() {
    echo ""
    echo "═══════════════════════════════════════════════════════════"
    echo "  RELAY #$RELAY_NUMBER СОЗДАН"
    echo "═══════════════════════════════════════════════════════════"
    echo ""
    echo "  VM:          $VM_NAME"
    echo "  IP:          $RELAY_IP"
    echo "  SSH:         ssh -i $SSH_KEY $SSH_USER@$RELAY_IP"
    echo "  Зона:        $YC_ZONE"
    echo "  Ресурсы:     ${YC_CORES} vCPU ${YC_CORE_FRACTION}%, ${YC_MEMORY}GB RAM, ${YC_DISK_SIZE}GB HDD"
    echo ""
    echo "  Маршруты:"
    for route in "${RELAY_ROUTES[@]}"; do
        IFS='|' read -r sni target_ip inbound_tag obhod_name <<< "$route"
        obhod_name="${obhod_name//\{N\}/$RELAY_NUMBER}"
        echo "    🛡 $obhod_name → $target_ip (SNI: $sni)"
    done
    echo ""
    echo "  Стоимость:   ~566 ₽/мес"
    echo ""
    echo "═══════════════════════════════════════════════════════════"
}

# ─── Основной поток ──────────────────────────────────────────────

main() {
    log "=== Создание relay-сервера в Yandex Cloud ==="
    echo ""

    check_deps
    ensure_ssh_key
    auto_detect_relay_number

    reserve_static_ip
    create_vm

    if ! $DRY_RUN; then
        wait_ssh_ready
    fi

    install_nginx
    configure_nginx_relay
    verify_relay

    get_marzban_token
    add_marzban_hosts

    print_summary

    log "Готово! Клиенты увидят новые ОБХОД серверы при обновлении подписки."
}

main "$@"
