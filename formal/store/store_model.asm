format ELF64 executable 3
entry start

include 'store_state.inc'
include 'store_actions.inc'
include 'store_generate.inc'
include 'store_emit.inc'

segment readable executable
start:
	call	store_generate
	call	store_emit
	mov	edi, eax
	mov	rax, 02000001h
	syscall

segment readable writeable
emit_sstate db 'SADD SStates s'
emit_sstate_id db '00000000',10
emit_sstate_end:
emit_sinv db 'SADD SInv s'
emit_sinv_id db '00000000',10
emit_sinv_end:
emit_sinitial db 'SADD SInitial s'
emit_sinitial_id db '00000000',10
emit_sinitial_end:
emit_stransition db 'RADD STransition s'
emit_stransition_parent db '00000000'
db ' s'
emit_stransition_child db '00000000',10
emit_stransition_end:

store_mutation_mode db STORE_MUTATION
align 4
store_state_count rd 1
store_transition_count rd 1
store_parent rb SSTATE_SIZE
store_child rb SSTATE_SIZE
store_transition_parent rd 1024
store_transition_child rd 1024
