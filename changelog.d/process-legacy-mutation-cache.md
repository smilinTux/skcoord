### Performance

- `CardStore` now shares one parse of the legacy mutation overlay (archive index plus `card_events`) per process. The cache is validated by every file's exact name, size, mtime and inode, so any append forces a reparse. Each fold had been re-reading and re-validating the whole overlay. On chiap01 one `sknoded --once` tick drops from about 39s of CPU to 2s (2026-10-10).
