Extract the key findings, decisions, and next steps from this conversation into a todo item on the current workstream.

Steps:
1. Review the full conversation and identify:
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

4. Run the command:
   ```
   orch distill crystallize --text "<task summary>" --context "<detailed context>"
   ```

   The `ORCH_WS_ID` env var is set automatically — no need to pass `--ws-id`.

5. Confirm to the user what was saved.
