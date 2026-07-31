; Transition relation for the bounded Agentlog 0.2 model.
; rdi=parent, esi=action, rdx=child; rax=1 valid / 0 invalid.

apply_action:
	push	rbx
	push	r12
	push	r13
	push	r14
	mov	r12, rdi
	mov	r13d, esi
	mov	r14, rdx
	mov	rdi, r14
	mov	rsi, r12
	call	state_copy
	cmp	r13d, ACTION_CREATE_RUN
	je	.create_run
	cmp	r13d, ACTION_APPEND_START
	je	.append_start
	cmp	r13d, ACTION_REACTION
	je	.reaction
	cmp	r13d, ACTION_MODEL_ANSWER
	je	.model_answer
	cmp	r13d, ACTION_MODEL_TOOL
	je	.model_tool
	cmp	r13d, ACTION_MODEL_FAILURE
	je	.model_failure
	cmp	r13d, ACTION_TOOL_SUCCESS
	je	.tool_success
	cmp	r13d, ACTION_TOOL_FAILURE
	je	.tool_failure
	cmp	r13d, ACTION_FORCE_COMPLETE
	je	.force_complete
	cmp	r13d, ACTION_FORCE_FAIL
	je	.force_fail
	cmp	r13d, ACTION_RESTART
	je	.restart
	jmp	.invalid

.create_run:
	cmp	byte [r12 + STATE_HISTORY_LEN_OFF], 0
	jne	.invalid
	cmp	byte [r12 + STATE_DEFINITION_OFF], 0
	jne	.invalid
	mov	byte [r14 + STATE_DEFINITION_OFF], 1
if MODEL_MUTATION = MUTATION_RESOURCE_MISMATCH
	mov	byte [r14 + STATE_RESERVED_OFF], 0
else
	mov	byte [r14 + STATE_RESERVED_OFF], 1
end if
	mov	byte [r14 + STATE_STATUS_OFF], STATUS_ACTIVE
	mov	rdi, r14
	mov	esi, EV_RUN_CREATED
	xor	edx, edx
	xor	ecx, ecx
	xor	r8d, r8d
	call	state_append_event
	jmp	.result

.append_start:
	cmp	byte [r12 + STATE_STATUS_OFF], STATUS_ACTIVE
	jne	.invalid
	cmp	byte [r12 + STATE_HISTORY_LEN_OFF], 1
	jne	.invalid
	mov	rdi, r14
	mov	esi, EV_START
	xor	edx, edx
	xor	ecx, ecx
	xor	r8d, r8d
	call	state_append_event
	jmp	.result

.reaction:
	movzx	ebx, byte [r12 + STATE_REACTION_CP_OFF]
	inc	ebx
	movzx	eax, byte [r12 + STATE_HISTORY_LEN_OFF]
	cmp	ebx, eax
	ja	.invalid
	mov	[r14 + STATE_REACTION_CP_OFF], bl
	cmp	byte [r12 + STATE_STATUS_OFF], STATUS_ACTIVE
	jne	.valid
	mov	rdi, r12
	mov	esi, ebx
	call	state_event_at
	test	rax, rax
	jz	.invalid
	movzx	ecx, byte [rax + EVENT_TYPE_OFF]
	cmp	ecx, EV_START
	je	.reaction_model_request
	cmp	ecx, EV_MODEL_SUCCEEDED_ANSWER
	je	.reaction_answer
	cmp	ecx, EV_MODEL_SUCCEEDED_TOOL
	je	.reaction_tool_request
	cmp	ecx, EV_TOOL_SUCCEEDED
	je	.reaction_model_continuation
	cmp	ecx, EV_MODEL_FAILED
	je	.reaction_fail
	cmp	ecx, EV_TOOL_FAILED
	je	.reaction_fail
	cmp	ecx, EV_MODEL_LIMIT
	je	.reaction_fail
	jmp	.valid

.reaction_model_request:
	movzx	ecx, byte [r14 + STATE_HISTORY_LEN_OFF]
	inc	ecx
	mov	rdi, r14
	mov	esi, EV_MODEL_REQUESTED
	mov	edx, ebx
	; request operation identity is its own 1-based position
	mov	r8d, 1                  ; model_step=1, tool_calls_used=0
	call	state_append_event
	jmp	.result

.reaction_answer:
	movzx	eax, byte [r14 + STATE_HISTORY_LEN_OFF]
	add	eax, 2
	cmp	eax, MODEL_BOUND
	ja	.invalid
	mov	rdi, r14
	mov	esi, EV_ANSWER_PRODUCED
	mov	edx, ebx
	xor	ecx, ecx
	xor	r8d, r8d
	call	state_append_event
	test	eax, eax
	jz	.invalid
	mov	rdi, r14
	mov	esi, EV_RUN_COMPLETED
	; Atomic reaction outputs are siblings: both are caused by the consumed
	; ModelSucceeded event, regardless of their order inside the append batch.
	mov	edx, ebx
	xor	ecx, ecx
	xor	r8d, r8d
	call	state_append_event
	mov	byte [r14 + STATE_STATUS_OFF], STATUS_COMPLETED
	jmp	.result

.reaction_tool_request:
	; Preserve model_step and increment the independent tool_calls_used field.
	movzx	r8d, byte [rax + EVENT_FLAGS_OFF]
	mov	eax, r8d
	shr	eax, LOOP_TOOL_SHIFT
	cmp	eax, MODEL_MAX_TOOL_CALLS
	jae	.reaction_limit
	add	r8d, 1 shl LOOP_TOOL_SHIFT
	movzx	ecx, byte [r14 + STATE_HISTORY_LEN_OFF]
	inc	ecx
	mov	rdi, r14
	mov	esi, EV_TOOL_REQUESTED
	mov	edx, ebx
	call	state_append_event
	jmp	.result

.reaction_model_continuation:
	mov	rdi, r12
	mov	esi, ebx
	call	state_event_at
	movzx	r8d, byte [rax + EVENT_FLAGS_OFF]
	mov	eax, r8d
	and	eax, LOOP_MODEL_MASK
	cmp	eax, MODEL_MAX_MODEL_STEPS
	jae	.reaction_limit
	inc	r8d
	movzx	ecx, byte [r14 + STATE_HISTORY_LEN_OFF]
	inc	ecx
	mov	rdi, r14
	mov	esi, EV_MODEL_REQUESTED
	mov	edx, ebx
	call	state_append_event
	jmp	.result
.reaction_limit:
	mov	rdi, r14
	mov	esi, EV_MODEL_LIMIT
	mov	edx, ebx
	xor	ecx, ecx
	xor	r8d, r8d
	call	state_append_event
	jmp	.result

.reaction_fail:
	mov	rdi, r14
	mov	esi, EV_RUN_FAILED
	mov	edx, ebx
	xor	ecx, ecx
	xor	r8d, r8d
	call	state_append_event
	test	eax, eax
	jz	.invalid
	mov	byte [r14 + STATE_STATUS_OFF], STATUS_FAILED
	jmp	.valid

.model_answer:
	mov	r13d, EV_MODEL_SUCCEEDED_ANSWER
	jmp	.model_effect
.model_tool:
	mov	r13d, EV_MODEL_SUCCEEDED_TOOL
	jmp	.model_effect
.model_failure:
	mov	r13d, EV_MODEL_FAILED
.model_effect:
	call	.effect_next
	test	rax, rax
	jz	.invalid
	cmp	byte [rax + EVENT_TYPE_OFF], EV_MODEL_REQUESTED
	jne	.valid
	cmp	byte [r12 + STATE_STATUS_OFF], STATUS_ACTIVE
	jne	.valid
	inc	byte [r14 + STATE_MODEL_INVOCATIONS_OFF]
	movzx	edx, byte [r14 + STATE_EFFECT_CP_OFF]
	movzx	ecx, byte [rax + EVENT_OPERATION_OFF]
	movzx	r8d, byte [rax + EVENT_FLAGS_OFF]
if MODEL_MUTATION = MUTATION_RESULT_WITHOUT_REQUEST
	xor	edx, edx
end if
if MODEL_MUTATION = MUTATION_OPERATION_MISMATCH
	inc	ecx
end if
	mov	rdi, r14
	mov	esi, r13d
	call	state_append_event
	test	eax, eax
	jz	.invalid
if MODEL_MUTATION = MUTATION_DUPLICATE_RESULT
	mov	rdi, r12
	mov	esi, ebx
	call	state_event_at
	movzx	ecx, byte [rax + EVENT_OPERATION_OFF]
	mov	rdi, r14
	mov	esi, r13d
	mov	edx, ebx
	xor	r8d, r8d
	call	state_append_event
end if
	jmp	.result

.tool_success:
	mov	r13d, EV_TOOL_SUCCEEDED
	jmp	.tool_effect
.tool_failure:
	mov	r13d, EV_TOOL_FAILED
.tool_effect:
	call	.effect_next
	test	rax, rax
	jz	.invalid
	cmp	byte [rax + EVENT_TYPE_OFF], EV_TOOL_REQUESTED
	jne	.valid
	cmp	byte [r12 + STATE_STATUS_OFF], STATUS_ACTIVE
	jne	.valid
	inc	byte [r14 + STATE_TOOL_INVOCATIONS_OFF]
	movzx	edx, byte [r14 + STATE_EFFECT_CP_OFF]
	movzx	ecx, byte [rax + EVENT_OPERATION_OFF]
	movzx	r8d, byte [rax + EVENT_FLAGS_OFF]
	mov	rdi, r14
	mov	esi, r13d
	call	state_append_event
	jmp	.result

; Return the event at effect_cp+1 after advancing child's checkpoint.
.effect_next:
	movzx	ebx, byte [r12 + STATE_EFFECT_CP_OFF]
	inc	ebx
	movzx	eax, byte [r12 + STATE_HISTORY_LEN_OFF]
	cmp	ebx, eax
	ja	.effect_missing
	mov	[r14 + STATE_EFFECT_CP_OFF], bl
	mov	rdi, r12
	mov	esi, ebx
	call	state_event_at
	ret
.effect_missing:
	xor	eax, eax
	ret

.force_complete:
	mov	r13d, EV_RUN_COMPLETED
	mov	ebx, STATUS_COMPLETED
	jmp	.force_terminal
.force_fail:
	mov	r13d, EV_RUN_FAILED
	mov	ebx, STATUS_FAILED
.force_terminal:
	cmp	byte [r12 + STATE_DEFINITION_OFF], 0
	je	.invalid
.force_has_definition:
if MODEL_MUTATION <> MUTATION_SECOND_TERMINAL
	mov	rdi, r12
	call	state_terminal_count
	test	eax, eax
	jnz	.invalid
else
	; Counterexample search stops expanding after the first bad successor.
	; A state with one terminal may produce the mutant's second terminal;
	; a state already containing two terminals has no further Force edge.
	mov	rdi, r12
	call	state_terminal_count
	cmp	eax, 2
	jae	.invalid
end if
	mov	rdi, r14
	mov	esi, r13d
	xor	edx, edx
	xor	ecx, ecx
	xor	r8d, r8d
	call	state_append_event
	test	eax, eax
	jz	.invalid
	mov	[r14 + STATE_STATUS_OFF], bl
	jmp	.valid

.restart:
if MODEL_MUTATION = MUTATION_HISTORY_REWRITE
	cmp	byte [r14 + STATE_HISTORY_LEN_OFF], 0
	je	.valid
	mov	byte [r14 + STATE_HISTORY_OFF + EVENT_TYPE_OFF], EV_START
end if
if MODEL_MUTATION = MUTATION_CHECKPOINT_ROLLBACK
	cmp	byte [r14 + STATE_REACTION_CP_OFF], 0
	je	.valid
	dec	byte [r14 + STATE_REACTION_CP_OFF]
end if
if MODEL_MUTATION = MUTATION_TERMINAL_PROGRESSION
	mov	rdi, r12
	call	state_terminal_count
	test	eax, eax
	jz	.valid
	movzx	esi, byte [r12 + STATE_HISTORY_LEN_OFF]
	mov	rdi, r12
	call	state_event_at
	cmp	byte [rax + EVENT_TYPE_OFF], EV_ANSWER_PRODUCED
	je	.valid
	mov	rdi, r14
	mov	esi, EV_ANSWER_PRODUCED
	xor	edx, edx
	xor	ecx, ecx
	xor	r8d, r8d
	call	state_append_event
	jmp	.result
end if
	jmp	.valid

.result:
	test	eax, eax
	jz	.invalid
.valid:
	mov	eax, 1
	jmp	.out
.invalid:
	xor	eax, eax
.out:
	pop	r14
	pop	r13
	pop	r12
	pop	rbx
	ret
