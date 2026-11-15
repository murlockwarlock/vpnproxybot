import pytest

from ops.darimiru.install_email_auth_nginx import INCLUDE, render_config


def test_adds_include_only_to_darimiru_https_server():
    source = '''server {
    server_name darimiru.ru www.darimiru.ru;
    listen 8444 ssl;
    location /profile {
        proxy_pass http://127.0.0.1:8900;
    }
}
server {
    server_name darimiru.xyz;
    listen 8444 ssl;
    location /profile {
        proxy_pass http://127.0.0.1:8900;
    }
}
'''
    updated = render_config(source)
    assert updated.count(INCLUDE) == 1
    first, second = updated.split('server_name darimiru.xyz;')
    assert INCLUDE in first and INCLUDE not in second
    assert updated.replace('    ' + INCLUDE + '\n\n', '') == source


def test_refuses_ambiguous_or_already_installed_configuration():
    block = '''server {
    server_name darimiru.ru www.darimiru.ru;
    listen 8444 ssl;
    location /profile { proxy_pass http://127.0.0.1:8900; }
}
'''
    with pytest.raises(ValueError):
        render_config(block + block)
    with pytest.raises(ValueError):
        render_config(render_config(block))
    with pytest.raises(ValueError):
        render_config(block.replace('listen 8444 ssl;', 'listen 80;'))
