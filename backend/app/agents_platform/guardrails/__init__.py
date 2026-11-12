"""
Guardrails for the v2 assistant (docs/multi-agent/04).

    normalize    Unicode clean-up before anything inspects the text
    injection    fast-path score for jailbreak and prompt-injection attempts
    pii          detection, masking and the vault for personal data typed into the chat
    input_guard  normalise, score, mask: one verdict per question (gates G1)
    classifier   optional model check for grey-zone inputs
    abuse        repeated blocked inputs lead to a cool-down
    output_guard claims a model must not make, personal data that must not leave (gate G8)
    policy       which agent may call which tool (gate G5)
"""
