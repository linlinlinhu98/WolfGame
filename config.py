# -*- coding: utf-8 -*-
"""Tunable rules options for the werewolf game.

ALLOW_R1_ABSTAIN: first-round abstain option (无信息不处决 convention).
  True  = agents may vote "abstain" in round 1: the vote model adds
          "abstain" to its Literal choices, majority_vote counts only real
          votes, and the ledger records abstains per voter for analysis.
  False = standard rules: everyone must vote an alive player (the baseline
          for all prior 40-game stats).
  Rationale: R1 zero-info bandwagons were the #1 measured village-loss
  pattern (S09: R1 lynch of a villager + witch night-1 blind poison ->
  village lost in 1 round). Human conventions treat a first-day no-lynch
  as acceptable; here it is an explicit, documented rules knob.
"""

ALLOW_R1_ABSTAIN = True


# --- Wolf self-consistency archive (2026-10-11) ---
# When True, each werewolf agent keeps a SpeechSummary of its OWN public
# speeches and gets a "your prior statements" block prepended to the
# discussion prompt: identity claims (悍跳), dated check-chain claims,
# accusations/defenses, and stated-vote-vs-actual-vote mismatches, with
# deterministic contradiction flags (dead-player checks, flip-flopped
# votes). Purpose: the wolf's #1 measurable exposure is forgetting its own
# lies across days (fabricated check chains drift from the ledger, votes
# contradict the 投票意向 line). Fair by design: it does NOT reveal any
# hidden info — only what the wolf itself said publicly, which everyone
# already heard; it just fixes the wolf's memory of its own words.
WOLF_SELF_CONSISTENCY = True
