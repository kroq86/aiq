abstract_hex32:
	push	rbx
	push	rcx
	push	rdx
	mov	ebx, eax
	mov	ecx, 8
.loop:
	mov	edx, ebx
	shr	edx, 28
	cmp	dl, 9
	jbe	.digit
	add	dl, 'a' - 10
	jmp	.store
.digit:
	add	dl, '0'
.store:
	mov	[rdi], dl
	inc	rdi
	shl	ebx, 4
	loop	.loop
	pop	rdx
	pop	rcx
	pop	rbx
	ret

abstract_write:
	test	edx, edx
	jz	.success
	mov	eax, 02000004h
	mov	edi, 1
	syscall
.success:
	xor	eax, eax
	ret

abstract_emit:
	push	rbx
	push	r12
	xor	r12d, r12d
.states:
	cmp	r12d, [abstract_state_count]
	jae	.transitions
	mov	eax, r12d
	lea	rdi, [emit_astate_id]
	call	abstract_hex32
	lea	rsi, [emit_astate]
	mov	edx, emit_astate_end - emit_astate
	call	abstract_write
	test	eax, eax
	jnz	.out
	mov	edi, r12d
	lea	rsi, [abstract_parent]
	call	astate_decode
	lea	rdi, [abstract_parent]
	call	astate_is_inv
	test	eax, eax
	jz	.initial
	mov	eax, r12d
	lea	rdi, [emit_ainv_id]
	call	abstract_hex32
	lea	rsi, [emit_ainv]
	mov	edx, emit_ainv_end - emit_ainv
	call	abstract_write
	test	eax, eax
	jnz	.out
.initial:
	lea	rdi, [abstract_parent]
	call	astate_is_initial
	test	eax, eax
	jz	.state_next
	mov	eax, r12d
	lea	rdi, [emit_ainitial_id]
	call	abstract_hex32
	lea	rsi, [emit_ainitial]
	mov	edx, emit_ainitial_end - emit_ainitial
	call	abstract_write
	test	eax, eax
	jnz	.out
.state_next:
	inc	r12d
	jmp	.states
.transitions:
	xor	r12d, r12d
.transition_loop:
	cmp	r12d, [abstract_transition_count]
	jae	.bad
	mov	eax, [abstract_transition_parent + r12 * 4]
	lea	rdi, [emit_atransition_parent]
	call	abstract_hex32
	mov	eax, [abstract_transition_child + r12 * 4]
	lea	rdi, [emit_atransition_child]
	call	abstract_hex32
	lea	rsi, [emit_atransition]
	mov	edx, emit_atransition_end - emit_atransition
	call	abstract_write
	test	eax, eax
	jnz	.out
	inc	r12d
	jmp	.transition_loop
.bad:
	xor	r12d, r12d
.bad_loop:
	cmp	r12d, [abstract_bad_edge_count]
	jae	.done
	mov	eax, [abstract_bad_edges + r12 * 4]
	lea	rdi, [emit_bad_transition_id]
	call	abstract_hex32
	lea	rsi, [emit_bad_transition]
	mov	edx, emit_bad_transition_end - emit_bad_transition
	call	abstract_write
	test	eax, eax
	jnz	.out
	inc	r12d
	jmp	.bad_loop
.done:
	xor	eax, eax
.out:
	pop	r12
	pop	rbx
	ret
