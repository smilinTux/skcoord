# 3c65c0fc diagnostic stop result

Date: 2026-09-22

Disposition: **STOPPED BEFORE SOURCE OR RUNTIME CHANGE**

The exact TDD required a supported read-only scan before implementation and
required work to stop if another rejected row appeared. The scan used the
reviewed public `scan_overlay_records` API from SKCoord commit
`64e8bfa9b668f7727030798e72fde44cbd42bc0a`. It did not inspect or print raw
JSONL bytes.

The scanner bound source generation
`b112e3420390a82165ea21bbd588e3a0994652aed888d42c0d9511186b55b95a` and
returned three rejected rows:

1. Physical line `10977`, SHA256
   `68c15e4ef388ed88d6a1879cab32a6b0ff76c44d6efc64c43e24c381b14a10eb`,
   card hint `ae44a693`, action hint `verdict`. The nonsecret schema error
   reports reserved fields `column` and `description`.
2. Physical line `19797`, SHA256
   `521dea6229a40e43d35ec12b9399f6d836ecad9a8c8d440d0b0df1b118c554a6`,
   card hint `086ea05c`, action hint `verdict`. The nonsecret schema error
   reports legacy field names and missing current required fields.
3. Newly discovered physical line `28501`, SHA256
   `dac89ec03055b114fe25b3afb58ef277fa320d754afad22e17b08412f3561d68`,
   card hint `e8f3a5b7`, action hint `verdict`. The nonsecret schema error
   reports forbidden extra fields `verdict`, `evidence_sha256`,
   `blocked_on`, `referent`, and `attempted`.

The supported diagnostic artifact is
`/home/skuser01/.skcapstone/evidence/work/3c65c0fc/supported-overlay-diagnostics.json`,
SHA256 `c96cffc0d70891d470d275e03a3bf6657db86c0c9c69afffb638cb02866c8e17`.

Card `3c65c0fc` names only lines 10977 and 19797. Removing those two would
still leave line 28501 rejected and would correctly fail the strict-valid
output requirement. Work therefore stopped before adding a failing source
test, changing recovery code, building or installing a package, stopping a
service, creating a recovery plan, applying recovery, or changing any ledger
byte.

The next exact scope must bind all three rows and require one atomic ordered
recovery with separate custody artifacts. If a supported scan then finds any
additional rejected row, it must stop again rather than broaden at runtime.

