format ELF64 executable 3
entry start

include 'abstract_state.inc'
include 'abstract_actions.inc'
include 'abstract_transition.inc'
include 'abstract_invariants.inc'
include 'abstract_emit.inc'

segment readable executable
start:
	call	abstract_generate
	call	abstract_emit
	mov	edi, eax
	mov	rax, 02000001h
	syscall

segment readable writeable
emit_astate db 'SADD AStates a'
emit_astate_id db '00000000',10
emit_astate_end:
emit_ainv db 'SADD AInv a'
emit_ainv_id db '00000000',10
emit_ainv_end:
emit_ainitial db 'SADD AInitial a'
emit_ainitial_id db '00000000',10
emit_ainitial_end:
emit_atransition db 'RADD ATransition a'
emit_atransition_parent db '00000000'
db ' a'
emit_atransition_child db '00000000',10
emit_atransition_end:
emit_bad_transition db 'SADD BadTransition edge'
emit_bad_transition_id db '00000000',10
emit_bad_transition_end:

abstract_mutation_mode db ABSTRACT_MUTATION
align 4
abstract_error rd 1
abstract_state_count rd 1
abstract_transition_count rd 1
abstract_bad_edge_count rd 1
abstract_parent rb ASTATE_SIZE
abstract_child rb ASTATE_SIZE
abstract_expected rb ASTATE_SIZE
abstract_transition_parent rd 1024
abstract_transition_child rd 1024
abstract_transition_action rb 1024
abstract_bad_edges rd 1024
