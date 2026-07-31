AA_START_LOW equ 0
AA_START_ADVANCE equ 1
AA_MODEL_ANSWER equ 2
AA_MODEL_TOOL_LOW equ 3
AA_MODEL_TOOL_ADVANCE equ 4
AA_MODEL_FAILURE equ 5
AA_TOOL_SUCCESS_LOW equ 6
AA_TOOL_SUCCESS_ADVANCE equ 7
AA_TOOL_FAILURE equ 8
AA_LIMIT_FAILURE equ 9
AA_RESTART equ 10
AA_FORCE_COMPLETE equ 11
AA_FORCE_FAIL equ 12
AA_COUNT equ 13

; rdi=parent, esi=action, rdx=child; eax=1 valid.
abstract_apply:
	push	rbx
	push	r12
	push	r13
	mov	r12, rdi
	mov	r13d, esi
	mov	rbx, rdx
	mov	rdi, rbx
	mov	rsi, r12
	call	astate_copy
	cmp	r13d, AA_RESTART
	je	.valid
	cmp	r13d, AA_FORCE_COMPLETE
	je	.complete
	cmp	r13d, AA_FORCE_FAIL
	je	.fail
	movzx	eax, byte [r12 + ASTATE_PHASE_OFF]
	cmp	eax, PHASE_COMPLETED
	je	.invalid
	cmp	eax, PHASE_FAILED
	je	.invalid
	cmp	eax, PHASE_IDLE
	je	.idle
	cmp	eax, PHASE_MODEL_PENDING
	je	.model
	cmp	eax, PHASE_TOOL_PENDING
	je	.tool
	cmp	eax, PHASE_LIMIT
	je	.limit
	jmp	.invalid
.idle:
	cmp	r13d, AA_START_LOW
	je	.start_low
	cmp	r13d, AA_START_ADVANCE
	je	.start_advance
	jmp	.invalid
.start_low:
	cmp	byte [r12 + ASTATE_MODEL_CLASS_OFF], CLASS_LOW
	jne	.invalid
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_MODEL_PENDING
	jmp	.valid
.start_advance:
	movzx	eax, byte [r12 + ASTATE_MODEL_CLASS_OFF]
	cmp	eax, CLASS_LOW
	je	.start_to_before
	cmp	eax, CLASS_BEFORE
	jne	.invalid
	mov	byte [rbx + ASTATE_MODEL_CLASS_OFF], CLASS_AT
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_MODEL_PENDING
	jmp	.valid
.start_to_before:
	mov	byte [rbx + ASTATE_MODEL_CLASS_OFF], CLASS_BEFORE
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_MODEL_PENDING
	jmp	.valid
.model:
	cmp	r13d, AA_MODEL_ANSWER
	je	.complete
	cmp	r13d, AA_MODEL_FAILURE
	je	.fail
	cmp	r13d, AA_MODEL_TOOL_LOW
	je	.model_tool_low
	cmp	r13d, AA_MODEL_TOOL_ADVANCE
	je	.model_tool_advance
	jmp	.invalid
.model_tool_low:
	cmp	byte [r12 + ASTATE_TOOL_CLASS_OFF], CLASS_AT
	je	.tool_limit
	cmp	byte [r12 + ASTATE_TOOL_CLASS_OFF], CLASS_LOW
	jne	.invalid
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_TOOL_PENDING
	cmp	byte [abstract_mutation_mode], AMUT_TOOL_UPDATES_MODEL
	jne	.no_tool_updates_model
	mov	byte [rbx + ASTATE_MODEL_CLASS_OFF], CLASS_BEFORE
.no_tool_updates_model:
	jmp	.valid
.model_tool_advance:
	movzx	eax, byte [r12 + ASTATE_TOOL_CLASS_OFF]
	cmp	eax, CLASS_AT
	je	.tool_limit_mutable
	cmp	eax, CLASS_LOW
	je	.tool_to_before
	cmp	eax, CLASS_BEFORE
	jne	.invalid
	cmp	byte [abstract_mutation_mode], AMUT_BEFORE_STAYS
	jne	.tool_before_normal
	mov	byte [rbx + ASTATE_TOOL_CLASS_OFF], CLASS_BEFORE
	jmp	.tool_before_done
.tool_before_normal:
	mov	byte [rbx + ASTATE_TOOL_CLASS_OFF], CLASS_AT
.tool_before_done:
	jmp	.tool_advance_finish
.tool_to_before:
	mov	byte [rbx + ASTATE_TOOL_CLASS_OFF], CLASS_BEFORE
.tool_advance_finish:
	cmp	byte [abstract_mutation_mode], AMUT_TOOL_RESET
	jne	.no_tool_reset
	mov	byte [rbx + ASTATE_TOOL_CLASS_OFF], CLASS_LOW
.no_tool_reset:
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_TOOL_PENDING
	jmp	.valid
.tool_limit_mutable:
	cmp	byte [abstract_mutation_mode], AMUT_AT_ALLOWS
	jne	.tool_limit
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_TOOL_PENDING
	jmp	.valid
.tool_limit:
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_LIMIT
	jmp	.valid
.tool:
	cmp	r13d, AA_TOOL_FAILURE
	je	.fail
	cmp	r13d, AA_TOOL_SUCCESS_LOW
	je	.tool_success_low
	cmp	r13d, AA_TOOL_SUCCESS_ADVANCE
	je	.tool_success_advance
	jmp	.invalid
.tool_success_low:
	cmp	byte [r12 + ASTATE_MODEL_CLASS_OFF], CLASS_AT
	je	.model_limit
	cmp	byte [r12 + ASTATE_MODEL_CLASS_OFF], CLASS_LOW
	jne	.invalid
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_MODEL_PENDING
	jmp	.valid
.tool_success_advance:
	movzx	eax, byte [r12 + ASTATE_MODEL_CLASS_OFF]
	cmp	eax, CLASS_AT
	je	.model_limit_mutable
	cmp	eax, CLASS_LOW
	je	.model_to_before
	cmp	eax, CLASS_BEFORE
	jne	.invalid
	mov	byte [rbx + ASTATE_MODEL_CLASS_OFF], CLASS_AT
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_MODEL_PENDING
	cmp	byte [abstract_mutation_mode], AMUT_MODEL_UPDATES_TOOL
	jne	.no_model_updates_tool
	mov	byte [rbx + ASTATE_TOOL_CLASS_OFF], CLASS_AT
.no_model_updates_tool:
	jmp	.valid
.model_to_before:
	mov	byte [rbx + ASTATE_MODEL_CLASS_OFF], CLASS_BEFORE
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_MODEL_PENDING
	jmp	.valid
.model_limit_mutable:
	cmp	byte [abstract_mutation_mode], AMUT_AT_ALLOWS
	jne	.model_limit
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_MODEL_PENDING
	jmp	.valid
.model_limit:
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_LIMIT
	jmp	.valid
.limit:
	cmp	r13d, AA_LIMIT_FAILURE
	jne	.invalid
.fail:
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_FAILED
	jmp	.valid
.complete:
	mov	byte [rbx + ASTATE_PHASE_OFF], PHASE_COMPLETED
.valid:
	mov	eax, 1
	jmp	.out
.invalid:
	xor	eax, eax
.out:
	pop	r13
	pop	r12
	pop	rbx
	ret
