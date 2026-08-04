---
name: apply
description: Build the application pack and fill the form as far as policy allows. Use for "apply to this" (with a URL or a role from the report).
---
# /apply — pack, fill, human submits
1. Fetch the live posting + application form. Check for employer AI-policy
   clauses; surface any verbatim before proceeding.
2. Build the pack: every field answer from profile.yaml (identity, work auth,
   EEO per their answer set), tailored free-texts from claims.md in their
   voice, resume choice. Show it.
3. Tier the form:
   - T1 (greenhouse/lever/ashby-class, no account): fill via Claude-in-Chrome.
     MECHANICS: text fields via form_input; EVERY dropdown by click - open,
     click a LABELED option, confirm the x-chip appears (values without the
     chip are dropped at submit); checkboxes by click; location autocompletes
     by real keystrokes + clicking the suggestion; after any failed submit,
     re-verify every field (failures wipe committed values). Full visual
     sweep before the submit button.
   - T2 (account-walled: Workday, Avature, iCIMS portals): hand the user the
     pack; co-drive user-present after they sign in. NEVER create accounts.
   - T3 (captcha-hostile): pack only.
4. STOP at the submit button. The human reviews and clicks it (the "I
   certify" click is theirs). Agent-click only if the user is present and
   says so for THIS application. Captcha/OTP: user relays codes themselves.
5. Verify the confirmation page. Log APPLIED in tracker.md with date, link,
   comp, resume used, and any commitments made on the form (AI clauses,
   disclosures).
