# senate.gov fixtures

senate.gov blocks scripted clients (`403 Forbidden`), so these were read in a real browser session rather than downloaded:

- `vote_menu_119_1.xml`: the session-1 vote menu, trimmed.
- Five vote files: bill passage with a VP tie-breaker (H.R. 1, roll 372), cloture on a bill (617), an amendment (616), cloture on a nomination (659), and a nomination with a VP tie-breaker (Hegseth, roll 15).

Element names, element order and values (question, title, majority requirement, document, amendment, counts, tie-breaker, and every senator's LIS ID, party, state and vote) were transcribed as published, then re-serialized locally. Senator names in `member_full`, `first_name` and `last_name` were filled from the crosswalk, since the parser does not use them. The rebuilt files are within about 1% of the size of the originals.

Some vote files (rolls 524, 614 and 646) kept returning 403 even in the browser, so the motion-to-table, motion-to-proceed and decision-of-the-chair cases are covered by question strings from the vote menu in `test_classify.py` instead of full vote files.
