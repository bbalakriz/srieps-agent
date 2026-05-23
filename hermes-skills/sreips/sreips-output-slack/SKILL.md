---
name: sreips-output-slack
description: Format RCA JSON for Slack and Mattermost markdown enrichment blocks.
version: 1.0.0
metadata:
  hermes:
    tags: [SREIPS, Slack, Mattermost]
---

# Slack / Mattermost output

## Format rules

- Use single asterisks for bold (Slack style), not double.
- Use bullet character for lists.
- Put root cause and recommended actions in clearly labeled sections.
- Include KCS links as `<url|title>` when possible.

## Section order

1. Summary
2. Root cause
3. Evidence (short bullets)
4. Recommended actions (immediate first)
5. Enterprise KB excerpts
6. KCS articles with links
