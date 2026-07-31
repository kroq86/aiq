; State invariant is astate_is_inv from abstract_state.inc.
; Transition validity is checked by replaying each generated edge with
; abstract_mutation_mode=AMUT_NONE in abstract_transition.inc. A mutant changes
; only the produced successor; the common oracle emits BadTransition.
