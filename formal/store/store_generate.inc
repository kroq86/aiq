store_generate:
	push	r12
	push	r13
	push	r14
	mov	dword [store_state_count], SSTATE_COUNT
	mov	dword [store_transition_count], 0
	xor	r12d, r12d
.state:
	cmp	r12d, SSTATE_COUNT
	jae	.done
	mov	edi, r12d
	lea	rsi, [store_parent]
	call	sstate_decode
	lea	rdi, [store_parent]
	call	sstate_is_inv
	test	eax, eax
	jz	.next_state
	xor	r13d, r13d
.action:
	cmp	r13d, SA_COUNT
	jae	.next_state
	lea	rdi, [store_parent]
	mov	esi, r13d
	lea	rdx, [store_child]
	call	store_apply
	test	eax, eax
	jz	.next_action
	lea	rdi, [store_child]
	call	sstate_id
	mov	r14d, eax
	mov	eax, [store_transition_count]
	mov	[store_transition_parent + rax * 4], r12d
	mov	[store_transition_child + rax * 4], r14d
	inc	dword [store_transition_count]
.next_action:
	inc	r13d
	jmp	.action
.next_state:
	inc	r12d
	jmp	.state
.done:
	pop	r14
	pop	r13
	pop	r12
	ret
