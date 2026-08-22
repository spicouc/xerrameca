"""Conversation presets for the Xerrameca wizard (data, not control flow)."""

from __future__ import annotations

from .dto import ConversationPreset

PRESETS: dict[str, ConversationPreset] = {
    "conversation": ConversationPreset(
        key="conversation",
        label="Conversa",
        instruction=(
            "Conversa federada estàndard entre dos agents. Intercanvi lliure "
            "per arribar a un acord o conclusió sobre l'objectiu plantejat."
        ),
        default_role_a="proposer",
        default_role_b="reviewer",
        completion_instruction=(
            "Produeix una conclusió consensuada o acord explícit sobre l'objectiu."
        ),
    ),
    "brainstorm": ConversationPreset(
        key="brainstorm",
        label="Brainstorm",
        instruction=(
            "Sessió d'ideació oberta. Genera tantes propostes com sigui possible "
            "sense jutjar-les, després sintetitza els eixos principals."
        ),
        default_role_a="proposer",
        default_role_b="researcher",
        completion_instruction=(
            "Sintetitza una llista prioritzada d'idees sense descartar-ne cap."
        ),
    ),
    "debate": ConversationPreset(
        key="debate",
        label="Debat",
        instruction=(
            "Debat estructurat: cada agent defensa una postura oposada sobre "
            "l'objectiu i enfronta els arguments de l'altre."
        ),
        default_role_a="proposer",
        default_role_b="critic",
        completion_instruction=(
            "Llista els punts acordats i els punts en disputa oberta."
        ),
    ),
    "critical_review": ConversationPreset(
        key="critical_review",
        label="Revisió",
        instruction=(
            "Revisió crítica d'una proposta o artefacte. L'agent peer examina "
            "defectes, riscos i millores abans de donar llum verda."
        ),
        default_role_a="executor",
        default_role_b="critic",
        completion_instruction=(
            "Emfatitza els defectes trobats i si la proposta és apta o no."
        ),
    ),
    "decision": ConversationPreset(
        key="decision",
        label="Decisió",
        instruction=(
            "Procés de decisió: avalua opcions i recomana la millor via "
            "fonamentada en els criteris de l'objectiu."
        ),
        default_role_a="researcher",
        default_role_b="supervisor",
        completion_instruction=(
            "Recomana una decisió única amb el seu raonament."
        ),
    ),
    "task": ConversationPreset(
        key="task",
        label="Tasca",
        instruction=(
            "Execució de tasca coordinada: un agent proposa i l'altre executa "
            "o valida el lliurament esperat."
        ),
        default_role_a="proposer",
        default_role_b="executor",
        completion_instruction=(
            "Confirma el lliurament obtingut o el motiu de bloqueig."
        ),
    ),
}

VALID_ROLES = (
    "proposer",
    "reviewer",
    "researcher",
    "critic",
    "executor",
    "supervisor",
    "custom",
)

VALID_ROUNDS = (3, 5, 6, 10)

VALID_OUTPUT_MODES = ("summary", "live", "silent")


def get_preset(key: str) -> ConversationPreset | None:
    return PRESETS.get(key)
