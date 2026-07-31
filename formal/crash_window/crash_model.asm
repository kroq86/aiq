format ELF64 executable 3
entry start

include 'crash_state.inc'
include 'crash_actions.inc'
include 'crash_generate.inc'
include 'crash_emit.inc'

segment readable executable
start:
	call	crash_generate
	call	crash_emit
	mov	edi, eax
	mov	rax, 02000001h
	syscall

segment readable writeable
emit_cstate db 'SADD CStates c'
emit_cstate_id db '00000000',10
emit_cstate_end:
emit_cinv db 'SADD CInv c'
emit_cinv_id db '00000000',10
emit_cinv_end:
emit_cinitial db 'SADD CInitial c'
emit_cinitial_id db '00000000',10
emit_cinitial_end:
emit_ctransition db 'RADD CTransition c'
emit_ctransition_parent db '00000000'
db ' c'
emit_ctransition_child db '00000000',10
emit_ctransition_end:

crash_mutation_mode db CRASH_MUTATION
align 4
crash_state_count rd 1
crash_transition_count rd 1
crash_parent rb CSTATE_SIZE
crash_child rb CSTATE_SIZE
crash_transition_parent rd 1024
crash_transition_child rd 1024
