; Enumerate all invariant states and successors. No history bound is involved.
crash_generate:
	push	r12
	push	r13
	push	r14
	mov	dword [crash_state_count], CSTATE_COUNT
	mov	dword [crash_transition_count], 0
	xor	r12d, r12d
.state:
	cmp	r12d, CSTATE_COUNT
	jae	.done
	mov	edi, r12d
	lea	rsi, [crash_parent]
	call	cstate_decode
	lea	rdi, [crash_parent]
	call	cstate_is_inv
	test	eax, eax
	jz	.next_state
	xor	r13d, r13d
.action:
	cmp	r13d, CA_COUNT
	jae	.next_state
	lea	rdi, [crash_parent]
	mov	esi, r13d
	lea	rdx, [crash_child]
	call	crash_apply
	test	eax, eax
	jz	.next_action
	lea	rdi, [crash_child]
	call	cstate_id
	mov	r14d, eax
	mov	eax, [crash_transition_count]
	mov	[crash_transition_parent + rax * 4], r12d
	mov	[crash_transition_child + rax * 4], r14d
	inc	dword [crash_transition_count]
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
