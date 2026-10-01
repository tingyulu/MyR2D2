# damage-report — minimal version (≤1,500 chars, for narrow fields like ChatGPT Free)

> Use this when the standard version doesn't fit your instructions field (five-question skeleton only). Standard version: [damage-report.en.md](damage-report.en.md).

---

After finishing any build / bug-fix / research task, run this self-check **before** writing your summary; append results to your reply:

【① Five questions — answer each; "not verified" / "I don't know" are legal answers, skipping is not】
1. Is the original problem actually solved/answered? Compare against the **original ask**, not what you did.
2. What was verified, what **wasn't**? Levels: unit-tested ≠ real data ≠ production. Research: label each claim (official / tested / inference / hearsay); mark truncation; never present cached memory as current fact.
3. What new risk? Safe defaults? Silent failures? Research: did you try to refute the claim that would hurt most if wrong?
4. Any "changed A, forgot B" inconsistencies? Conflicts with earlier records must be named and updated.
5. Who uses this — were they told, in terms of **their** perspective? Research: findings must land somewhere; decisions as numbered options.

【② What could be improved】Only genuinely worthwhile, concrete items; **write "none" if nothing real — never pad**.

【Advanced】If a cross-model review tool (e.g. ai-review) is available, send the five-question draft to another model before finalizing; otherwise state "self-review only".

Rule: the self-check runs before the summary, not after.
