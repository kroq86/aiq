; Exhaustive bounded graph construction over packed durable states.

; edi = state id; rax = state pointer
model_state_ptr:
	mov	eax, edi
	imul	rax, STATE_SIZE
	lea	rax, [model_states + rax]
	ret

; rdi = candidate state; rax = id or -1
model_find_state:
	push	rbx
	push	r12
	mov	r12, rdi
	xor	ebx, ebx
.loop:
	cmp	ebx, [model_state_count]
	jae	.missing
	mov	edi, ebx
	call	model_state_ptr
	mov	rdi, rax
	mov	rsi, r12
	call	state_equal
	test	eax, eax
	jnz	.found
	inc	ebx
	jmp	.loop
.found:
	mov	eax, ebx
	jmp	.out
.missing:
	mov	rax, -1
.out:
	pop	r12
	pop	rbx
	ret

; rdi = candidate; rax = id, edx = 1 inserted / 0 existing
model_add_state:
	push	rbx
	push	r12
	mov	r12, rdi
	call	model_find_state
	cmp	rax, -1
	jne	.existing
	mov	ebx, [model_state_count]
	cmp	ebx, MODEL_MAX_STATES
	jae	.overflow
	mov	edi, ebx
	call	model_state_ptr
	mov	rdi, rax
	mov	rsi, r12
	call	state_copy
	mov	byte [model_expandable + rbx], 1
	inc	dword [model_state_count]
	mov	eax, ebx
	mov	edx, 1
	jmp	.out
.existing:
	xor	edx, edx
	jmp	.out
.overflow:
	mov	dword [model_error], 1
	mov	rax, -1
	xor	edx, edx
.out:
	pop	r12
	pop	rbx
	ret

; edi=parent, esi=child, edx=action
model_add_transition:
	mov	eax, [model_transition_count]
	cmp	eax, MODEL_MAX_TRANSITIONS
	jae	.overflow
	mov	[model_transition_parent + rax * 4], edi
	mov	[model_transition_child + rax * 4], esi
	mov	[model_transition_action + rax], dl
	inc	dword [model_transition_count]
	ret
.overflow:
	mov	dword [model_error], 2
	ret

model_explore:
	push	rbx
	push	r12
	push	r13
	push	r14
	mov	dword [model_state_count], 0
	mov	dword [model_transition_count], 0
	mov	dword [model_error], 0
	lea	rdi, [model_scratch]
	call	state_zero
	lea	rdi, [model_scratch]
	call	model_add_state
	xor	r12d, r12d
.state_loop:
	cmp	r12d, [model_state_count]
	jae	.done
	cmp	byte [model_expandable + r12], 0
	je	.next_state
	xor	r13d, r13d
.action_loop:
	cmp	r13d, ACTION_COUNT
	jae	.next_state
	mov	edi, r12d
	call	model_state_ptr
	mov	rdi, rax
	mov	esi, r13d
	lea	rdx, [model_scratch]
	call	apply_action
	test	eax, eax
	jz	.next_action
	lea	rdi, [model_scratch]
	call	model_add_state
	cmp	rax, -1
	je	.done
	mov	ebx, eax
	test	edx, edx
	jz	.have_witness
	mov	[model_parent + rbx * 4], r12d
	mov	[model_parent_action + rbx], r13b
if MODEL_MUTATION <> MUTATION_NONE
	lea	rdi, [model_scratch]
	call	model_check_state
	mov	r14d, eax
	mov	edi, r12d
	call	model_state_ptr
	mov	rdi, rax
	lea	rsi, [model_scratch]
	call	model_check_edge
	or	r14d, eax
	test	r14d, r14d
	jz	.have_witness
	mov	byte [model_expandable + rbx], 0
end if
.have_witness:
	mov	edi, r12d
	mov	esi, ebx
	mov	edx, r13d
	call	model_add_transition
	cmp	dword [model_error], 0
	jne	.done
.next_action:
	inc	r13d
	jmp	.action_loop
.next_state:
	inc	r12d
	jmp	.state_loop
.done:
	pop	r14
	pop	r13
	pop	r12
	pop	rbx
	ret
