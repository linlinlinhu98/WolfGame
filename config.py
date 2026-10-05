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
