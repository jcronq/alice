---
title: Don't ask Owner to run sudo commands
aliases: [no sudo, no root commands]
tags: [feedback]
created: 2026-04-24
---

# Don't ask Owner to run sudo commands

> **tl;dr** Hit a sudo wall? Find another path before surfacing it.

## Rule

Don't ask Owner to run `sudo` or root-level commands on Alice's behalf. Find a different approach or do without.

## Why

Owner wants Alice to complete work independently. Handing off admin steps breaks that flow and turns a task into a ticket.

## How to apply

- If hitting a sudo wall, pause and look for alternatives first (user-level systemd, workarounds, alternative approach).
- When sudo is truly unavoidable (system service units, `/etc/` configs): do as much as possible around it, state the specific command that would be needed, and keep moving.
- Don't block on the owner's input for a shell-level chore.

## Related

- [[owner]]
