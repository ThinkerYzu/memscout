# memscout

A lightweight, read-only runtime data-collection framework for live Linux processes.

Inspect the internal state of a running application or service by reading its
memory directly through `/proc/<pid>/mem` — no debugger, never stopping the target.

    memscout modules <pid>                      # loaded ELF modules + build-ids
    memscout resolve <pid> <symbol>             # symbol -> runtime address
    memscout scan <pid> <vtable-symbol> [OFF:TYPE:NAME ...]   # find + decode objects
    memscout dump <pid> <addr>                  # print an object's class + annotated slots

Project docs (spec, design, handoff) live in the task repo under
`proj_docs/memscout/`.
