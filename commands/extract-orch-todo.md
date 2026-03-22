Extract findings from this conversation into a todo item on the current workstream.

If the user provided focus text after the command (e.g. `/user:extract fix the pane leak`), scope the extraction to that topic only. If no focus was given, ask the user: "What should I extract? The whole conversation or a specific topic?" Do NOT assume the entire conversation is relevant.

Steps:
1. Identify (within the scoped topic):
   - What was investigated or explored
   - What was decided or concluded
   - What concrete work remains to be done
   - Key files, approaches, or gotchas discovered

2. Write a short, actionable task summary (1 line) for the `--text` flag — this becomes the todo title.

3. Write a detailed context block for the `--context` flag that a fresh Claude session could use to pick up this work cold, without re-investigating. Include:
   - Problem statement
   - What was tried / what worked / what didn't
   - The chosen approach and why
   - Specific files, functions, or commands relevant
   - Any risks or edge cases to watch for

4. Show the user the draft (task summary + context) and ask for confirmation before saving.

5. Run the command:
   ```
   orch distill crystallize --text "<task summary>" --context "<detailed context>"
   ```

   The `ORCH_WS_ID` env var is set automatically — no need to pass `--ws-id`.

6. Confirm to the user what was saved.
