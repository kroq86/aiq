; Serialize the generated graph as one setdb SADD/RADD fact stream.

; eax=value, rdi=8-byte destination
model_hex32:
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

; rsi=bytes, ecx=count, rdi=2*count destination
model_hex_bytes:
	push	rbx
	push	r12
	mov	r12, rsi
	xor	ebx, ebx
.loop:
	cmp	ebx, ecx
	jae	.done
	movzx	eax, byte [r12 + rbx]
	mov	edx, eax
	shr	eax, 4
	mov	al, [model_hex_digits + rax]
	mov	[rdi], al
	inc	rdi
	and	edx, 0fh
	mov	dl, [model_hex_digits + rdx]
	mov	[rdi], dl
	inc	rdi
	inc	ebx
	jmp	.loop
.done:
	pop	r12
	pop	rbx
	ret

; rdi=concrete packed state; eax=finite ModelLoopModel state id.
; For the fixed 2/2 witness: 0->low, 1->before, 2+->at.
model_abstract_id:
	push	rbx
	push	r12
	push	r13
	push	r14
	push	r15
	mov	r12, rdi
	xor	r13d, r13d             ; phase=idle
	xor	r14d, r14d             ; model class=low
	xor	r15d, r15d             ; tool class=low
	movzx	eax, byte [r12 + STATE_STATUS_OFF]
	cmp	eax, STATUS_COMPLETED
	je	.completed
	cmp	eax, STATUS_FAILED
	je	.failed
	movzx	ebx, byte [r12 + STATE_HISTORY_LEN_OFF]
.scan:
	test	ebx, ebx
	jz	.compose
	mov	rdi, r12
	mov	esi, ebx
	call	state_event_at
	movzx	ecx, byte [rax + EVENT_FLAGS_OFF]
	test	ecx, ecx
	jz	.kind
	test	r14d, r14d
	jnz	.kind
	mov	edx, ecx
	and	edx, LOOP_MODEL_MASK
	cmp	edx, 1
	je	.model_before
	cmp	edx, 2
	jb	.tool_class
	mov	r14d, 2
	jmp	.tool_class
.model_before:
	mov	r14d, 1
.tool_class:
	shr	ecx, LOOP_TOOL_SHIFT
	cmp	ecx, 1
	je	.tool_before
	cmp	ecx, 2
	jb	.kind
	mov	r15d, 2
	jmp	.kind
.tool_before:
	mov	r15d, 1
.kind:
	cmp	r13d, 0
	jne	.next
	movzx	ecx, byte [rax + EVENT_TYPE_OFF]
	cmp	ecx, EV_MODEL_LIMIT
	je	.limit
	cmp	ecx, EV_MODEL_REQUESTED
	je	.model_pending
	cmp	ecx, EV_TOOL_REQUESTED
	je	.tool_pending
	jmp	.next
.limit:
	mov	r13d, 3
	jmp	.next
.model_pending:
	mov	r13d, 1
	jmp	.next
.tool_pending:
	mov	r13d, 2
.next:
	dec	ebx
	jmp	.scan
.completed:
	mov	r13d, 4
	jmp	.scan_counters
.failed:
	mov	r13d, 5
.scan_counters:
	movzx	ebx, byte [r12 + STATE_HISTORY_LEN_OFF]
	jmp	.scan
.compose:
	mov	eax, r13d
	imul	eax, 9
	mov	ecx, r14d
	imul	ecx, 3
	add	eax, ecx
	add	eax, r15d
	pop	r15
	pop	r14
	pop	r13
	pop	r12
	pop	rbx
	ret

; rsi=buffer, edx=length
model_write:
	test	edx, edx
	jz	.done
.loop:
	mov	eax, 02000004h
	mov	edi, 1
	syscall
	jc	.error
	test	rax, rax
	jle	.failed
	add	rsi, rax
	sub	rdx, rax
	jnz	.loop
.done:
	ret
.error:
	cmp	eax, 4                 ; EINTR
	je	.loop
.failed:
	mov	dword [model_error], 3
	ret

model_emit_graph:
	push	rbx
	push	r12
	push	r13
	push	r14
	push	r15
	xor	r12d, r12d
.states:
	cmp	r12d, [model_state_count]
	jae	.initial
	mov	eax, r12d
	lea	rdi, [emit_state_id]
	call	model_hex32
	lea	rsi, [emit_state]
	mov	edx, emit_state_end - emit_state
	call	model_write
	mov	eax, r12d
	lea	rdi, [emit_encoding_state]
	call	model_hex32
	mov	edi, r12d
	call	model_state_ptr
	mov	rsi, rax
	mov	ecx, STATE_SIZE
	lea	rdi, [emit_encoding_hex]
	call	model_hex_bytes
	lea	rsi, [emit_encoding]
	mov	edx, emit_encoding_prefix_end - emit_encoding
	call	model_write
	lea	rsi, [emit_encoding_hex]
	mov	edx, STATE_SIZE * 2
	call	model_write
	lea	rsi, [emit_newline]
	mov	edx, 1
	call	model_write
	mov	eax, r12d
	lea	rdi, [emit_beta_concrete]
	call	model_hex32
	mov	edi, r12d
	call	model_state_ptr
	mov	rdi, rax
	call	model_abstract_id
	lea	rdi, [emit_beta_abstract]
	call	model_hex32
	lea	rsi, [emit_beta]
	mov	edx, emit_beta_end - emit_beta
	call	model_write
	inc	r12d
	jmp	.states
.initial:
	lea	rsi, [emit_initial]
	mov	edx, emit_initial_end - emit_initial
	call	model_write
	xor	r12d, r12d
.transitions:
	cmp	r12d, [model_transition_count]
	jae	.parents
	mov	eax, [model_transition_parent + r12 * 4]
	lea	rdi, [emit_transition_parent]
	call	model_hex32
	mov	eax, [model_transition_child + r12 * 4]
	lea	rdi, [emit_transition_child]
	call	model_hex32
	lea	rsi, [emit_transition]
	mov	edx, emit_transition_end - emit_transition
	call	model_write
	inc	r12d
	jmp	.transitions
.parents:
	mov	r12d, 1
.parent_loop:
	cmp	r12d, [model_state_count]
	jae	.done
	mov	eax, r12d
	lea	rdi, [emit_parent_child]
	call	model_hex32
	mov	eax, [model_parent + r12 * 4]
	lea	rdi, [emit_parent_parent]
	call	model_hex32
	lea	rsi, [emit_parent]
	mov	edx, emit_parent_end - emit_parent
	call	model_write
	mov	eax, r12d
	lea	rdi, [emit_action_child]
	call	model_hex32
	movzx	eax, byte [model_parent_action + r12]
	lea	rdi, [emit_action_id]
	call	model_hex32
	lea	rsi, [emit_action]
	mov	edx, emit_action_end - emit_action
	call	model_write
	inc	r12d
	jmp	.parent_loop
.done:
	mov	r12d, 1
.violation_witnesses:
	cmp	r12d, [model_state_count]
	jae	.bad_sets
	cmp	byte [model_bad_mask + r12], 0
	je	.violation_next
	mov	eax, r12d
	lea	rdi, [emit_violation_child]
	call	model_hex32
	mov	eax, [model_violation_parent + r12 * 4]
	lea	rdi, [emit_violation_parent_id]
	call	model_hex32
	lea	rsi, [emit_violation_parent]
	mov	edx, emit_violation_parent_end - emit_violation_parent
	call	model_write
	mov	eax, r12d
	lea	rdi, [emit_violation_action_child]
	call	model_hex32
	movzx	eax, byte [model_violation_action + r12]
	lea	rdi, [emit_violation_action_id]
	call	model_hex32
	lea	rsi, [emit_violation_action]
	mov	edx, emit_violation_action_end - emit_violation_action
	call	model_write
.violation_next:
	inc	r12d
	jmp	.violation_witnesses
.bad_sets:
	mov	r13d, BAD_HISTORY_APPEND_ONLY
	lea	r14, [emit_bad_history]
	mov	ebx, emit_bad_history_end - emit_bad_history
	lea	r15, [emit_bad_history_id]
	call	model_emit_bad_set
	mov	r13d, BAD_CHECKPOINT_MONOTONIC
	lea	r14, [emit_bad_checkpoint]
	mov	ebx, emit_bad_checkpoint_end - emit_bad_checkpoint
	lea	r15, [emit_bad_checkpoint_id]
	call	model_emit_bad_set
	mov	r13d, BAD_RESULT_HAS_REQUEST
	lea	r14, [emit_bad_request]
	mov	ebx, emit_bad_request_end - emit_bad_request
	lea	r15, [emit_bad_request_id]
	call	model_emit_bad_set
	mov	r13d, BAD_OPERATION_MISMATCH
	lea	r14, [emit_bad_operation]
	mov	ebx, emit_bad_operation_end - emit_bad_operation
	lea	r15, [emit_bad_operation_id]
	call	model_emit_bad_set
	mov	r13d, BAD_DUPLICATE_RESULT
	lea	r14, [emit_bad_result]
	mov	ebx, emit_bad_result_end - emit_bad_result
	lea	r15, [emit_bad_result_id]
	call	model_emit_bad_set
	mov	r13d, BAD_DUPLICATE_TERMINAL
	lea	r14, [emit_bad_terminal]
	mov	ebx, emit_bad_terminal_end - emit_bad_terminal
	lea	r15, [emit_bad_terminal_id]
	call	model_emit_bad_set
	mov	r13d, BAD_TERMINAL_NOT_ABSORBING
	lea	r14, [emit_bad_absorbing]
	mov	ebx, emit_bad_absorbing_end - emit_bad_absorbing
	lea	r15, [emit_bad_absorbing_id]
	call	model_emit_bad_set
	mov	r13d, BAD_DEFINITION_RESOURCE
	lea	r14, [emit_bad_definition]
	mov	ebx, emit_bad_definition_end - emit_bad_definition
	lea	r15, [emit_bad_definition_id]
	call	model_emit_bad_set
	pop	r15
	pop	r14
	pop	r13
	pop	r12
	pop	rbx
	ret

; r13d=bit, r14=template, ebx=length, r15=id field
model_emit_bad_set:
	xor	r12d, r12d
.loop:
	cmp	r12d, [model_state_count]
	jae	.done
	movzx	eax, byte [model_bad_mask + r12]
	test	eax, r13d
	jz	.next
	mov	eax, r12d
	mov	rdi, r15
	call	model_hex32
	mov	rsi, r14
	mov	edx, ebx
	call	model_write
.next:
	inc	r12d
	jmp	.loop
.done:
	ret
