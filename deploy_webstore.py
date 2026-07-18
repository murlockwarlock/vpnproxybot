#!/usr/bin/env python3
"""Deploy the web store to the NL server (72.56.71.124).

Uploads webstore/ and .env.webstore, installs as a systemd service,
updates nginx config for darimiru.ru.
"""

import os
import sys
import stat

import paramiko

from scripts.deploy_guard import ensure_clean_git, load_local_env, require_env

NL_HOST = "72.56.71.124"
NL_USER = "root"
load_local_env(os.path.dirname(os.path.abspath(__file__)))
NL_PASS = require_env("NL_WEBSTORE_SSH_PASSWORD")

REMOTE_DIR = "/opt/webstore"
LOCAL_BASE = os.path.dirname(os.path.abspath(__file__))


def ssh_connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(NL_HOST, username=NL_USER, password=NL_PASS)
    return ssh


def run(ssh, cmd, check=True):
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode()
    err = stderr.read().decode()
    code = stdout.channel.recv_exit_status()
    if check and code != 0:
        print(f"  CMD FAILED ({code}): {cmd}")
        print(f"  stderr: {err}")
    return out, err, code


def upload_dir(sftp, local_dir, remote_dir):
    """Recursively upload a directory."""
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        sftp.mkdir(remote_dir)

    for item in os.listdir(local_dir):
        local_path = os.path.join(local_dir, item)
        remote_path = f"{remote_dir}/{item}"

        if os.path.isdir(local_path):
            upload_dir(sftp, local_path, remote_path)
        else:
            print(f"  Uploading {item}")
            sftp.put(local_path, remote_path)


def upload_shared_bot_services(sftp):
    """Upload shared bot services imported by the standalone webstore."""
    service_files = [
        "payment_logger.py",
        "provisioning_issues.py",
        "vhq_partner_api.py",
        "vhq_routing.py",
    ]
    for remote_dir in (f"{REMOTE_DIR}/bot", f"{REMOTE_DIR}/bot/services"):
        try:
            sftp.stat(remote_dir)
        except FileNotFoundError:
            sftp.mkdir(remote_dir)
    for filename in service_files:
        local_path = os.path.join(LOCAL_BASE, "bot", "services", filename)
        remote_path = f"{REMOTE_DIR}/bot/services/{filename}"
        print(f"  Uploading bot/services/{filename}")
        sftp.put(local_path, remote_path)


def main():
    revision = ensure_clean_git(LOCAL_BASE)
    print(f"Deploying web store to {NL_HOST}...")
    print(f"Revision: {revision}")
    ssh = ssh_connect()
    sftp = ssh.open_sftp()

    # 1. Create remote directory
    print("\n1. Creating remote directory...")
    run(ssh, f"mkdir -p {REMOTE_DIR}/webstore/templates")

    # 2. Upload webstore files
    print("\n2. Uploading webstore files...")
    upload_dir(sftp, os.path.join(LOCAL_BASE, "webstore"), f"{REMOTE_DIR}/webstore")

    sftp.file(f"{REMOTE_DIR}/REVISION", "w").write(revision + "\n")
    print(f"  Uploading REVISION {revision}")

    print("\n2a. Uploading shared bot service files...")
    upload_shared_bot_services(sftp)

    # 2b. Upload logo
    logo_path = os.path.join(LOCAL_BASE, "svg.jpeg")
    if os.path.exists(logo_path):
        print("  Uploading logo (svg.jpeg)...")
        sftp.put(logo_path, f"{REMOTE_DIR}/svg.jpeg")

    # 3. Upload .env.webstore
    print("\n3. Uploading .env.webstore...")
    local_env = os.path.join(LOCAL_BASE, ".env.webstore")
    if os.path.exists(local_env):
        # Check if remote .env.webstore exists - don't overwrite if it has credentials
        try:
            sftp.stat(f"{REMOTE_DIR}/.env.webstore")
            print("  .env.webstore already exists on server, skipping (won't overwrite credentials)")
        except FileNotFoundError:
            sftp.put(local_env, f"{REMOTE_DIR}/.env.webstore")
            print("  Uploaded .env.webstore")

    sftp.close()

    # 4. Create systemd service
    print("\n4. Setting up systemd service...")
    service_content = f"""[Unit]
Description=Web Store for darimiru.ru
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory={REMOTE_DIR}
ExecStart=/opt/anewkabot/venv/bin/python -m webstore
Restart=always
RestartSec=5
Environment=PYTHONPATH={REMOTE_DIR}

[Install]
WantedBy=multi-user.target
"""
    run(ssh, f"cat > /etc/systemd/system/webstore.service << 'SERVICEEOF'\n{service_content}SERVICEEOF")
    run(ssh, "systemctl daemon-reload")
    run(ssh, "systemctl enable webstore.service")
    run(ssh, "systemctl restart webstore.service")

    print("  Service started. Checking status...")
    out, _, _ = run(ssh, "systemctl is-active webstore.service", check=False)
    print(f"  Status darimiru: {out.strip()}")

    # Also restart webstore-loonapie (same code base, different env)
    out2, _, code2 = run(ssh, "systemctl is-active webstore-loonapie.service", check=False)
    if out2.strip() in ("active", "inactive"):
        run(ssh, "systemctl restart webstore-loonapie.service")
        out3, _, _ = run(ssh, "systemctl is-active webstore-loonapie.service", check=False)
        print(f"  Status loonapie: {out3.strip()}")
    else:
        print("  webstore-loonapie.service not found, skipping")

    # 5. Update nginx config for darimiru.ru
    print("\n5. Updating nginx config...")
    # Read current config
    stdin, stdout, stderr = ssh.exec_command("cat /etc/nginx/sites-enabled/darimiru")
    current_nginx = stdout.read().decode()
    original_nginx = current_nginx
    normalized_nginx = current_nginx.replace("listen 127.0.0.1:8444 ssl;", "listen 8444 ssl;")
    current_nginx = normalized_nginx

    has_api_route = "location /api/store/" in current_nginx
    has_buy_route = "location = /buy" in current_nginx
    has_profile_route = "location /profile" in current_nginx
    has_support_page_route = "location /support" in current_nginx
    has_support_api_route = "location /api/support/" in current_nginx
    has_support_ws_route = "location /ws/support/" in current_nginx
    has_external_8444_redirect = "if ($remote_addr != 127.0.0.1)" in current_nginx

    if (
        has_api_route
        and has_buy_route
        and has_profile_route
        and has_support_page_route
        and has_support_api_route
        and has_support_ws_route
        and has_external_8444_redirect
        and current_nginx == original_nginx
    ):
        print("  Nginx already has all webstore routes, skipping")
    else:
        route_blocks = []
        if not has_external_8444_redirect:
            current_nginx = current_nginx.replace(
                "    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;\n",
                "    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;\n\n"
                "    if ($remote_addr != 127.0.0.1) {\n"
                "        return 301 https://darimiru.ru$request_uri;\n"
                "    }\n",
            )

        if not has_api_route:
            route_blocks.append("""
    # Web store API
    location /api/store/ {
        proxy_pass http://127.0.0.1:8900;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
""")
        if "/pay/" not in current_nginx:
            route_blocks.append("""
    # Web store payment routes
    location /pay/ {
        proxy_pass http://127.0.0.1:8900;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
""")
        if not has_buy_route:
            route_blocks.append("""
    # Web store landing page
    location = /buy {
        proxy_pass http://127.0.0.1:8900/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
""")
        if not has_profile_route:
            route_blocks.append("""
    # Web store profile page
    location /profile {
        proxy_pass http://127.0.0.1:8900;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
""")
        if not has_support_page_route:
            route_blocks.append("""
    # Web store support chat pages
    location /support {
        proxy_pass http://127.0.0.1:8900;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
""")
        if not has_support_api_route:
            route_blocks.append("""
    # Web store support chat API
    location /api/support/ {
        proxy_pass http://127.0.0.1:8900;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
""")
        if not has_support_ws_route:
            route_blocks.append("""
    # Web store support chat WebSocket
    location /ws/support/ {
        proxy_pass http://127.0.0.1:8900;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
""")

        webstore_routes = "\n".join(route_blocks) + "\n"
        insert_markers = [
            "    # Marzban dashboard/API proxy\n    location / {",
            "    # Marzban dashboard/API proxy (moved from / to /dashboard/)\n    location /dashboard/ {",
            "    # Marzban API (needed for dashboard and internal calls)\n    location /api/ {",
        ]
        new_nginx = current_nginx
        for marker in insert_markers:
            if marker in current_nginx:
                new_nginx = current_nginx.replace(marker, webstore_routes + marker)
                break

        if new_nginx == current_nginx and current_nginx == original_nginx:
            print("  Could not find nginx insertion marker, skipping nginx update")
            ssh.close()
            sys.exit(1)

        # Write updated config
        run(ssh, f"cat > /etc/nginx/sites-enabled/darimiru << 'NGINXEOF'\n{new_nginx}NGINXEOF")

        # Test and reload
        out, err, code = run(ssh, "nginx -t", check=False)
        if code == 0:
            run(ssh, "systemctl reload nginx")
            print("  Nginx config updated and reloaded")
        else:
            print(f"  Nginx config test FAILED: {err}")
            # Restore original
            run(ssh, f"cat > /etc/nginx/sites-enabled/darimiru << 'NGINXEOF'\n{current_nginx}NGINXEOF")
            print("  Restored original nginx config")

    # 6. Check logs
    print("\n6. Checking webstore logs...")
    out, _, _ = run(ssh, "journalctl -u webstore.service -n 20 --no-pager", check=False)
    print(out)

    ssh.close()
    print("\nDone! Web store available at https://darimiru.ru/buy")
    print("Note: Payment buttons will be disabled until YooKassa credentials are added to .env.webstore")


if __name__ == "__main__":
    main()
