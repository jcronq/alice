# Tools

Operational snippets for running Alice. Not a script catalog — copy-paste these directly.

## Restart the viewer and verify

The viewer runs as the `alice-viewer` Docker container, bind-mounting `~/alice` read-only. On-disk edits are visible to the container's filesystem immediately, but the long-lived Python process keeps modules in RAM, so source changes only take effect after a container restart.

```sh
docker restart alice-viewer
sleep 3
curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:7777/interactions   # expect 200
curl -s http://localhost:7777/interactions | wc -c                              # expect ~390KB
```

If the status is 500, grab the traceback with `docker logs --since 5m alice-viewer`.
