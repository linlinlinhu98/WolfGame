# -*- coding: utf-8 -*-
"""The structured output models used in the werewolf game."""
from typing import Literal, Optional, Type

from pydantic import BaseModel, Field
from _vendor import AgentBase


# ── Reasoning Result Model (new - Phase 1) ───────────────────────────

class SuspectEntry(BaseModel):
    """A suspect with probability and reason."""
    name: str = Field(description="Player name")
    probability: float = Field(
        ge=0.0, le=1.0,
        description="Probability this player is a werewolf"
    )
    reason: str = Field(description="Specific reason for suspicion")


class ReasoningResultModel(BaseModel):
    """Structured output from the agent's internal reasoning step."""

    suspect_ranking: list[SuspectEntry] = Field(
        default_factory=list,
        description="Suspects ordered by probability descending"
    )
    trust_ranking: list[SuspectEntry] = Field(
        default_factory=list,
        description="Trusted players ordered by trust descending"
    )
    vote_plan: str = Field(
        default="",
        description="Who the agent plans to vote for"
    )
    strategy: str = Field(
        default="",
        description="Strategy description for this round"
    )
    should_reveal_role: bool = Field(
        default=False,
        description="Whether the agent should publicly claim its role"
    )
    internal_notes: str = Field(
        default="",
        description="Private reasoning chain"
    )


# ── Game Action Models ────────────────────────────────────────────────

class DiscussionModel(BaseModel):
    """The output format for discussion."""

    reach_agreement: bool = Field(
        description="Whether you have reached an agreement or not",
        default=False,
    )
    proposed_target: Optional[str] = Field(
        description="Proposed target for werewolf to kill",
        default=None,
    )
    speech: str = Field(
        description="Your detailed speech during the discussion",
        default="",
    )


def get_vote_model(agents: list[AgentBase]) -> Type[BaseModel]:
    """Get the vote model by player names."""
    if not agents:
        class DefaultVoteModel(BaseModel):
            vote: str = Field(description="Vote target name", default="")
        return DefaultVoteModel

    player_names = tuple(_.name for _ in agents)

    class VoteModel(BaseModel):
        """The vote output format."""
        vote: Literal[player_names] = Field(  # type: ignore
            description="You must vote! The name of the player you want to vote for",
        )

    return VoteModel


class WitchResurrectModel(BaseModel):
    """The output format for witch resurrect action."""
    resurrect: bool = Field(
        description="Whether you want to resurrect the player",
        default=False,
    )


def get_poison_model(agents: list[AgentBase]) -> Type[BaseModel]:
    """Get the poison model by player names."""
    if not agents:
        class DefaultPoisonModel(BaseModel):
            poison: bool = Field(default=False)
            name: Optional[str] = Field(default=None)
        return DefaultPoisonModel

    player_names = tuple(_.name for _ in agents)

    class WitchPoisonModel(BaseModel):
        """The output format for witch poison action."""
        poison: bool = Field(
            description="Do you want to use the poison potion",
            default=False,
        )
        name: Optional[Literal[player_names]] = Field(  # type: ignore
            description="The name of the player you want to poison",
            default=None,
        )

    return WitchPoisonModel


def get_seer_model(agents: list[AgentBase]) -> Type[BaseModel]:
    """Get the seer model by player names."""
    if not agents:
        class DefaultSeerModel(BaseModel):
            name: str = Field(default="")
        return DefaultSeerModel

    player_names = tuple(_.name for _ in agents)

    class SeerModel(BaseModel):
        """The output format for seer action."""
        name: Literal[player_names] = Field(  # type: ignore
            description="The name of the player you want to check",
        )

    return SeerModel


def get_hunter_model(agents: list[AgentBase]) -> Type[BaseModel]:
    """Get the hunter model by player agents."""
    if not agents:
        class DefaultHunterModel(BaseModel):
            shoot: bool = Field(default=False)
            name: Optional[str] = Field(default=None)
        return DefaultHunterModel

    player_names = tuple(_.name for _ in agents)

    class HunterModel(BaseModel):
        """The output format for hunter action."""
        shoot: bool = Field(
            description="Whether you want to use the shooting ability",
            default=False,
        )
        name: Optional[Literal[player_names]] = Field(  # type: ignore
            description="The name of the player you want to shoot",
            default=None,
        )

    return HunterModel