# ToteBot wall service

`tb-wall.service` runs the main race wall on core7070, listening on port 8787 with the same all-interface binding as the previous tmux launch. It reads replicated state and history, and needs no Betfair credentials or timer. The queue discovery timer runs separately on basecamp-vps.

Install from `~/botlab/totebot`:

```sh
install -D -m 644 scripts/tb-wall.service ~/.config/systemd/user/tb-wall.service
systemctl --user daemon-reload
```

Stop the old tmux wall process before enabling the service:

```sh
systemctl --user enable --now tb-wall.service
systemctl --user status tb-wall.service
journalctl --user -u tb-wall.service -n 50
```

Open http://core7070:8787. The separate race queue wall uses port 8792.

Live views poll the current URL every 15 seconds and patch only changed components. Applied filters remain in the request URL; unsaved filter edits, open disclosures, and scroll positions are preserved. Historical views do not poll. Failed requests leave the last data visible with a retry status. Background tabs pause polling and check immediately when visible again. Keep `tb_wall_poll.js` beside `tb_wall.py` when deploying. Polling reuses the existing HTML renderer; it still fetches full HTML but does not reload the browser page.
