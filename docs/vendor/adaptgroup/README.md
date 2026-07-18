# AdaptGroup API VPN documentation snapshot

Source: https://docs.adaptgroup.pro/docs/api-vpn

Saved on: 2026-06-01

This directory keeps local HTML snapshots of the AdaptGroup API VPN documentation
pages used by the bot/webstore integration:

- `api-vpn.html`
- `api-vpn-adaptgroup-vpn-api.html`
- `api-vpn-create-subscription.html`
- `api-vpn-renew-subscription.html`
- `api-vpn-freeze-subscription.html`
- `api-vpn-unfreeze-subscription.html`
- `api-vpn-upgrade-subscription.html`
- `api-vpn-purchase-traffic.html`
- `api-vpn-get-subscription-status.html`
- `api-vpn-get-devices.html`
- `api-vpn-get-requests.html`
- `api-vpn-delete-device.html`
- `api-vpn-list-plans.html`
- `api-vpn-plans.html`
- `api-vpn-subscription-endpoint.html`
- `api-vpn-subscriptions.html`
- `api-vpn-webhooks.html`

Relevant renewal behavior: `POST /subs/renew` renews an existing subscription by
`subscription_uuid`; it does not create a new subscription URL.
