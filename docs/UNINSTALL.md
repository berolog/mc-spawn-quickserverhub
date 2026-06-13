# Uninstalling

## From the bot (recommended)

Delete the machine in the Telegram bot. The bot shows exactly what will be removed and then
issues the `agent.uninstall` action. The agent:

1. removes every Minecraft container **it created** (matched by its own `mcspawn-server-*`
   name prefix) and their `_data` world volumes;
2. tears down playit (its tunnels + container + stored secret);
3. removes its own service/autostart entry and files (a detached step that survives the
   service stopping); on Windows it also `wsl --unregister`s the dedicated `mc-spawn` distro.

This requires `allow_agent_uninstall` in your policy (default on).

## Manually

Linux — root systemd:

```bash
systemctl disable --now mc-spawn-agent
rm -f /etc/systemd/system/mc-spawn-agent.service && systemctl daemon-reload
rm -rf /opt/mc-spawn-agent /etc/mc-spawn-agent ~/.mc-spawn
```

Linux — rootless systemd (`--user`):

```bash
systemctl --user disable --now mc-spawn-agent
rm -f ~/.config/systemd/user/mc-spawn-agent.service && systemctl --user daemon-reload
rm -rf ~/.local/share/mc-spawn-agent ~/.config/mc-spawn-agent ~/.mc-spawn
```

Linux — OpenRC: `rc-update del mc-spawn-agent; rc-service mc-spawn-agent stop; rm -f
/etc/init.d/mc-spawn-agent`. Nohup/crontab fallback: `kill "$(cat
~/.config/mc-spawn-agent/agent.pid)"` and remove the `@reboot` line (`crontab -e`).

Windows (PowerShell):

```powershell
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'mc-spawn-agent' -ErrorAction SilentlyContinue
schtasks /delete /tn mc-spawn-agent /f 2>$null
wsl --unregister mc-spawn 2>$null
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\mc-spawn-agent","$env:USERPROFILE\.mc-spawn" -ErrorAction SilentlyContinue
```

## Containers (if you removed the agent before deleting servers)

```bash
docker ps -a --filter name=^mcspawn-server- --format '{{.Names}}' | xargs -r docker rm -f
docker rm -f mc-spawn-playit 2>/dev/null
```

## Credentials only

To revoke this box without uninstalling: `python3 agent.py wipe-creds` removes the agent secret
(and playit key); re-enroll later with a fresh token from the bot.

## playit account

playit has no API to delete the agent *registration* itself (it's a dashboard action and we only
hold the agent secret). After teardown the now-offline, tunnel-less agent entry remains in your
playit account — harmless; remove it from playit.gg's dashboard if you wish.
