# Feedback Triage — maintainer-agent workflow

This directory is the single intake point for issues/feedback about hive-cli,
filed by **other agents or users** via `hive feedback`. The agent that owns this
repo (working in the checkout) is responsible for triaging it.

## Layout

```
feedback/
  inbox/        # one Markdown file per report (frontmatter + body). New reports land here.
  archive/      # the original long-form session reports that seeded the roadmap (read-only history).
  INDEX.md      # auto-generated table of all inbox entries (do NOT hand-edit).
  ROADMAP.md    # synthesized plan: themes, common-vs-special, phase mapping, per-issue status.
  TRIAGE.md     # this file.
```

## Filing (for any agent / user)

```bash
hive feedback "list 的 CMD 列被截断, 看不出 shard 后缀"          # quick one-liner
hive feedback submit --title "OOM-blind dispatch" \
    --severity high --tags scheduler,oom --task 3536,3541 \
    --file /tmp/details.md
```

Each submit auto-captures: hive version (from CHANGELOG), a `queue.json` state
histogram, a `node_monitor.json` status histogram, and the tail of any
referenced `task-<id>.log`. This is appended under `## Auto-captured context` so
the maintainer can reproduce without round-trips.

Storage resolves to `$HIVE_FEEDBACK_DIR`, else the **source checkout** recorded by
`install.sh` in `<install-dir>/.source_checkout`, else `<repo>/feedback`, else
`$HIVE_DIR/feedback`. Agents run the *installed* copy, so without that pointer their
reports landed in `~/.local/share/hive-cli/feedback/inbox/` where the maintainer never
looked and where the next `install.sh` (`rsync --delete`) would have wiped them (this
happened to #11–#33). The installer now also rescues any inbox entries found in the
install dir into the checkout and never deletes `feedback/inbox/`.

## Triage loop (maintainer agent)

1. `hive feedback list --status open` — see what's new.
2. For each: `hive feedback show <id>`. Decide:
   - **Duplicate** of an existing theme → `hive feedback triage <id> --status duplicate --note "dup of #N / ROADMAP §X"`.
   - **Actionable** → map it to a ROADMAP theme (or add a new one), then
     `hive feedback triage <id> --status triaged --note "ROADMAP §<theme>, phase <n>"`.
   - **Won't fix / out of scope** → `--status wontfix --note "<reason>"`.
3. While implementing a fix, set `--status in-progress`; when shipped (code +
   docs + CHANGELOG), set `--status done --note "<commit/version>"`.
4. Keep `ROADMAP.md` in sync: every triaged report should be reachable from a
   theme there, and every theme should cite the reports that motivated it.

## Rules

- **Never silently drop feedback.** Every entry ends in a terminal status
  (`done` / `wontfix` / `duplicate`) with a `triage_note` explaining why.
- **Common before special.** Cluster reports by root cause first; fix the shared
  root (see ROADMAP "common roots") before per-report symptoms.
- **Don't break running work.** Daemons run from the *installed* copy, not the
  checkout — edit/test in the checkout; schema changes must be additive and
  default-safe; only restart daemons after the restart-safety fixes land. See
  `docs/architecture.md` and `CLAUDE.md`.
- A fix isn't `done` until the skill (`.claude/commands/hive.md`), `README*`, and
  `CHANGELOG.md` reflect the new behavior — other agents read those to learn hive.
