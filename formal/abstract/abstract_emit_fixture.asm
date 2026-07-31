ABSTRACT_MUTATION = 0

format ELF64 executable 3
entry start

include 'abstract_state.inc'
include 'abstract_emit.inc'

segment readable executable
start:
	mov	dword [abstract_state_count], 1
	mov	dword [abstract_transition_count], 1
	mov	dword [abstract_transition_parent], 0
	mov	dword [abstract_transition_child], 0
	mov	dword [abstract_bad_edge_count], 0
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
align 4
abstract_error rd 1
abstract_state_count rd 1
abstract_transition_count rd 1
abstract_bad_edge_count rd 1
abstract_parent rb ASTATE_SIZE
abstract_transition_parent rd 1
abstract_transition_child rd 1
abstract_bad_edges rd 1
