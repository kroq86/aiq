ABSTRACT_MUTATION = 0

format ELF64 executable 3
entry start

include 'abstract_state.inc'
include 'abstract_actions.inc'
include 'abstract_transition.inc'

segment readable executable
start:
	call	abstract_generate
	cmp	dword [abstract_state_count], ASTATE_COUNT
	jne	.bad_state_count
	cmp	dword [abstract_transition_count], 0
	jle	.bad_transition_count
	cmp	dword [abstract_transition_count], 219
	jne	.bad_transition_count
	cmp	dword [abstract_transition_count], 1024
	ja	.bad_capacity
	lea	rsi, [generate_ok]
	mov	edx, generate_ok_end - generate_ok
	call	diagnostic_write
	mov	rax, 02000001h
	xor	edi, edi
	syscall
.bad_state_count:
	mov	edi, 4
	jmp	.exit
.bad_transition_count:
	mov	edi, 4
	jmp	.exit
.bad_capacity:
	mov	edi, 3
.exit:
	mov	rax, 02000001h
	syscall

diagnostic_write:
	mov	eax, 02000004h
	mov	edi, 1
	syscall
	xor	eax, eax
	ret

segment readable writeable
generate_ok db 'GENERATE_OK states=54 transitions=219',10
generate_ok_end:
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
