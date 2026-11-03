"""Packaged PROMOP API surface for an embedding host (CancerBot ADR 0001).

The wheel ships models, derivation and the canonical writers. A host that mounts
the PROMOP patient widget also needs the endpoints the widget calls; those live
here rather than in ``patient_portal``, which is deliberately not packaged.

Nothing in this package holds host policy. Views expose seams the host overrides
(permissions, per-person write authorization, the record payload shape), so the
same clinical contract runs in place and embedded, with authorization supplied by
whoever owns the users.
"""
