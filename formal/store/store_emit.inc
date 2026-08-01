store_hex32:
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

store_write:
	test	edx, edx
	jz	.success
	mov	eax, 02000004h
	mov	edi, 1
	syscall
.success:
	xor	eax, eax
	ret

store_emit:
	push	r12
	xor	r12d, r12d
.states:
	cmp	r12d, [store_state_count]
	jae	.transitions
	mov	eax, r12d
	lea	rdi, [emit_sstate_id]
	call	store_hex32
	lea	rsi, [emit_sstate]
	mov	edx, emit_sstate_end - emit_sstate
	call	store_write
	mov	edi, r12d
	lea	rsi, [store_parent]
	call	sstate_decode
	lea	rdi, [store_parent]
	call	sstate_is_inv
	test	eax, eax
	jz	.initial
	mov	eax, r12d
	lea	rdi, [emit_sinv_id]
	call	store_hex32
	lea	rsi, [emit_sinv]
	mov	edx, emit_sinv_end - emit_sinv
	call	store_write
.initial:
	lea	rdi, [store_parent]
	call	sstate_is_initial
	test	eax, eax
	jz	.state_next
	mov	eax, r12d
	lea	rdi, [emit_sinitial_id]
	call	store_hex32
	lea	rsi, [emit_sinitial]
	mov	edx, emit_sinitial_end - emit_sinitial
	call	store_write
.state_next:
	inc	r12d
	jmp	.states
.transitions:
	xor	r12d, r12d
.transition_loop:
	cmp	r12d, [store_transition_count]
	jae	.done
	mov	eax, [store_transition_parent + r12 * 4]
	lea	rdi, [emit_stransition_parent]
	call	store_hex32
	mov	eax, [store_transition_child + r12 * 4]
	lea	rdi, [emit_stransition_child]
	call	store_hex32
	lea	rsi, [emit_stransition]
	mov	edx, emit_stransition_end - emit_stransition
	call	store_write
	inc	r12d
	jmp	.transition_loop
.done:
	xor	eax, eax
	pop	r12
	ret
