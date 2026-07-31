crash_hex32:
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

crash_write:
	test	edx, edx
	jz	.success
	mov	eax, 02000004h
	mov	edi, 1
	syscall
.success:
	xor	eax, eax
	ret

crash_emit:
	push	r12
	xor	r12d, r12d
.states:
	cmp	r12d, [crash_state_count]
	jae	.transitions
	mov	eax, r12d
	lea	rdi, [emit_cstate_id]
	call	crash_hex32
	lea	rsi, [emit_cstate]
	mov	edx, emit_cstate_end - emit_cstate
	call	crash_write
	mov	edi, r12d
	lea	rsi, [crash_parent]
	call	cstate_decode
	lea	rdi, [crash_parent]
	call	cstate_is_inv
	test	eax, eax
	jz	.initial
	mov	eax, r12d
	lea	rdi, [emit_cinv_id]
	call	crash_hex32
	lea	rsi, [emit_cinv]
	mov	edx, emit_cinv_end - emit_cinv
	call	crash_write
.initial:
	lea	rdi, [crash_parent]
	call	cstate_is_initial
	test	eax, eax
	jz	.state_next
	mov	eax, r12d
	lea	rdi, [emit_cinitial_id]
	call	crash_hex32
	lea	rsi, [emit_cinitial]
	mov	edx, emit_cinitial_end - emit_cinitial
	call	crash_write
.state_next:
	inc	r12d
	jmp	.states
.transitions:
	xor	r12d, r12d
.transition_loop:
	cmp	r12d, [crash_transition_count]
	jae	.done
	mov	eax, [crash_transition_parent + r12 * 4]
	lea	rdi, [emit_ctransition_parent]
	call	crash_hex32
	mov	eax, [crash_transition_child + r12 * 4]
	lea	rdi, [emit_ctransition_child]
	call	crash_hex32
	lea	rsi, [emit_ctransition]
	mov	edx, emit_ctransition_end - emit_ctransition
	call	crash_write
	inc	r12d
	jmp	.transition_loop
.done:
	xor	eax, eax
	pop	r12
	ret
