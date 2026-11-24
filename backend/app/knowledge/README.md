# Curated notes for fault-code explanations (Phase 4, step 3)

Short background the LLM may use when it explains a code: local conditions and practical
checks that a definition alone doesn't carry. The context builder picks the 2-3 most
relevant notes for each code (`app/services/dtc/notes.py`).

**SEED - validate (D4):** written from general knowledge for Wisdom to correct and extend
with his own diagnostic experience in Lagos. No vehicle or personal data belongs here.

Each note starts with a small header:

    ---
    title: ...
    codes: [P0171, P0174]        # codes it's written for (exact match scores highest)
    systems: [fuel_air]          # definition systems it applies to
    keywords: [vacuum, leak]     # words that also suggest it
    ---
