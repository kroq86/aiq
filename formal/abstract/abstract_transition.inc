; Enumerate every strengthened-invariant state and all abstract actions.

abstract_generate:
	push	rbx
	push	r12
	push	r13
	xor	r12d, r12d
	mov	dword [abstract_transition_count], 0
	mov	dword [abstract_bad_edge_count], 0
	mov	dword [abstract_state_count], ASTATE_COUNT
.state:
	cmp	r12d, ASTATE_COUNT
	jae	.done
	mov	edi, r12d
	lea	rsi, [abstract_parent]
	call	astate_decode
	lea	rdi, [abstract_parent]
	call	astate_is_inv
	test	eax, eax
	jz	.next_state
	xor	r13d, r13d
.action:
	cmp	r13d, AA_COUNT
	jae	.next_state
	lea	rdi, [abstract_parent]
	mov	esi, r13d
	lea	rdx, [abstract_child]
	call	abstract_apply
	test	eax, eax
	jz	.next_action
	lea	rdi, [abstract_child]
	call	astate_id
	mov	ebx, eax
	mov	eax, [abstract_transition_count]
	mov	[abstract_transition_parent + rax * 4], r12d
	mov	[abstract_transition_child + rax * 4], ebx
	mov	[abstract_transition_action + rax], r13b
	inc	dword [abstract_transition_count]

	; The common oracle evaluates the same edge against normal semantics.
	lea	rdi, [abstract_parent]
	mov	esi, r13d
	lea	rdx, [abstract_expected]
	push	rax
	call	abstract_apply_normal
	test	eax, eax
	jz	.bad
	lea	rdi, [abstract_child]
	lea	rsi, [abstract_expected]
	call	astate_equal
	test	eax, eax
	jnz	.good
.bad:
	mov	eax, [abstract_bad_edge_count]
	mov	edx, [abstract_transition_count]
	dec	edx
	mov	[abstract_bad_edges + rax * 4], edx
	inc	dword [abstract_bad_edge_count]
.good:
	pop	rax
.next_action:
	inc	r13d
	jmp	.action
.next_state:
	inc	r12d
	jmp	.state
.done:
	pop	r13
	pop	r12
	pop	rbx
	ret

; Normal semantics is provided by a second compiled copy of abstract_apply.
abstract_apply_normal:
	push	rbx
	mov	bl, [abstract_mutation_mode]
	mov	byte [abstract_mutation_mode], AMUT_NONE
	call	abstract_apply
	mov	[abstract_mutation_mode], bl
	pop	rbx
	ret
