BAD_HISTORY_APPEND_ONLY equ 1
BAD_CHECKPOINT_MONOTONIC equ 2
BAD_RESULT_HAS_REQUEST equ 4
BAD_OPERATION_MISMATCH equ 8
BAD_DUPLICATE_RESULT equ 16
BAD_DUPLICATE_TERMINAL equ 32
BAD_TERMINAL_NOT_ABSORBING equ 64
BAD_DEFINITION_RESOURCE equ 128

; rdi=parent, rsi=child; eax=edge violation mask
model_check_edge:
	push	rbx
	push	r12
	push	r13
	mov	r12, rdi
	mov	r13, rsi
	xor	ebx, ebx
	movzx	eax, byte [r12 + STATE_HISTORY_LEN_OFF]
	movzx	ecx, byte [r13 + STATE_HISTORY_LEN_OFF]
	cmp	ecx, eax
	jb	.bad_prefix
	mov	edx, eax
	imul	edx, EVENT_SIZE
	test	edx, edx
	jz	.check_cp
	lea	rdi, [r12 + STATE_HISTORY_OFF]
	lea	rsi, [r13 + STATE_HISTORY_OFF]
	mov	ecx, edx
	repe	cmpsb
	je	.check_cp
.bad_prefix:
	or	ebx, BAD_HISTORY_APPEND_ONLY
.check_cp:
	movzx	eax, byte [r12 + STATE_REACTION_CP_OFF]
	movzx	ecx, byte [r13 + STATE_REACTION_CP_OFF]
	cmp	ecx, eax
	jb	.bad_cp
	movzx	eax, byte [r12 + STATE_EFFECT_CP_OFF]
	movzx	ecx, byte [r13 + STATE_EFFECT_CP_OFF]
	cmp	ecx, eax
	jb	.bad_cp
	movzx	eax, byte [r13 + STATE_HISTORY_LEN_OFF]
	movzx	ecx, byte [r13 + STATE_REACTION_CP_OFF]
	cmp	ecx, eax
	ja	.bad_cp
	movzx	ecx, byte [r13 + STATE_EFFECT_CP_OFF]
	cmp	ecx, eax
	jbe	.check_terminal
.bad_cp:
	or	ebx, BAD_CHECKPOINT_MONOTONIC
.check_terminal:
	mov	rdi, r12
	call	state_terminal_count
	test	eax, eax
	jz	.done
	movzx	eax, byte [r12 + STATE_HISTORY_LEN_OFF]
	movzx	ecx, byte [r13 + STATE_HISTORY_LEN_OFF]
	cmp	eax, ecx
	jne	.bad_absorb
	imul	eax, EVENT_SIZE
	lea	rdi, [r12 + STATE_HISTORY_OFF]
	lea	rsi, [r13 + STATE_HISTORY_OFF]
	mov	ecx, eax
	repe	cmpsb
	je	.done
.bad_absorb:
	or	ebx, BAD_TERMINAL_NOT_ABSORBING
.done:
	mov	eax, ebx
	pop	r13
	pop	r12
	pop	rbx
	ret

; rdi=state; eax=state violation mask
model_check_state:
	push	rbx
	push	r12
	push	r13
	push	r14
	mov	r12, rdi
	xor	ebx, ebx
	mov	rdi, r12
	call	state_terminal_count
	cmp	eax, 1
	jbe	.scan
	or	ebx, BAD_DUPLICATE_TERMINAL
.scan:
	mov	r13d, 1
.event_loop:
	movzx	eax, byte [r12 + STATE_HISTORY_LEN_OFF]
	cmp	r13d, eax
	ja	.done
	mov	rdi, r12
	mov	esi, r13d
	call	state_event_at
	mov	r14, rax
	mov	al, [r14 + EVENT_TYPE_OFF]
	call	event_is_result
	test	eax, eax
	jz	.next
	movzx	r10d, byte [r14 + EVENT_CAUSE_OFF]
	test	r10d, r10d
	jz	.bad_request
	cmp	r10d, r13d
	jae	.bad_request
	mov	rdi, r12
	mov	esi, r10d
	call	state_event_at
	test	rax, rax
	jz	.bad_request
	mov	r11, rax
	mov	al, [r11 + EVENT_TYPE_OFF]
	call	event_is_request
	test	eax, eax
	jz	.bad_request
	movzx	eax, byte [r14 + EVENT_TYPE_OFF]
	cmp	eax, EV_TOOL_SUCCEEDED
	je	.expect_tool
	cmp	eax, EV_TOOL_FAILED
	je	.expect_tool
	cmp	byte [r11 + EVENT_TYPE_OFF], EV_MODEL_REQUESTED
	je	.check_operation
	jmp	.bad_request
.expect_tool:
	cmp	byte [r11 + EVENT_TYPE_OFF], EV_TOOL_REQUESTED
	je	.check_operation
.bad_request:
	or	ebx, BAD_RESULT_HAS_REQUEST
	jmp	.check_duplicate
.check_operation:
	movzx	eax, byte [r14 + EVENT_OPERATION_OFF]
	movzx	ecx, byte [r11 + EVENT_OPERATION_OFF]
	cmp	eax, ecx
	je	.check_duplicate
	or	ebx, BAD_OPERATION_MISMATCH
.check_duplicate:
	mov	rdi, r12
	mov	esi, r10d
	call	state_result_count_for_request
	cmp	eax, 1
	jbe	.check_definition
	or	ebx, BAD_DUPLICATE_RESULT
.check_definition:
	cmp	byte [r12 + STATE_DEFINITION_OFF], 1
	jne	.bad_definition
	cmp	byte [r12 + STATE_RESERVED_OFF], 1
	je	.next
.bad_definition:
	or	ebx, BAD_DEFINITION_RESOURCE
.next:
	inc	r13d
	jmp	.event_loop
.done:
	mov	eax, ebx
	pop	r14
	pop	r13
	pop	r12
	pop	rbx
	ret

model_check_all:
	push	rbx
	push	r12
	push	r13
	xor	r12d, r12d
.states:
	cmp	r12d, [model_state_count]
	jae	.edges_start
	mov	edi, r12d
	call	model_state_ptr
	mov	rdi, rax
	call	model_check_state
	mov	[model_bad_mask + r12], al
	test	eax, eax
	jz	.state_next
	test	r12d, r12d
	jz	.state_next
	mov	ecx, [model_parent + r12 * 4]
	mov	[model_violation_parent + r12 * 4], ecx
	mov	cl, [model_parent_action + r12]
	mov	[model_violation_action + r12], cl
.state_next:
	inc	r12d
	jmp	.states
.edges_start:
	xor	r12d, r12d
.edges:
	cmp	r12d, [model_transition_count]
	jae	.done
	mov	ebx, [model_transition_parent + r12 * 4]
	mov	r13d, [model_transition_child + r12 * 4]
	mov	edi, ebx
	call	model_state_ptr
	mov	rdi, rax
	mov	edi, r13d
	call	model_state_ptr
	mov	rsi, rax
	mov	edi, ebx
	call	model_state_ptr
	mov	rdi, rax
	call	model_check_edge
	test	eax, eax
	jz	.edge_store
	mov	[model_violation_parent + r13 * 4], ebx
	mov	cl, [model_transition_action + r12]
	mov	[model_violation_action + r13], cl
.edge_store:
	or	[model_bad_mask + r13], al
	inc	r12d
	jmp	.edges
.done:
	pop	r13
	pop	r12
	pop	rbx
	ret
