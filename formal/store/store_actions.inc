SA_APPEND equ 0
SA_COMMIT_BATCH_ONE equ 1
SA_COMMIT_BATCH_TWO equ 2
SA_CHECKPOINT_ONE equ 3
SA_CHECKPOINT_MANY_TO_ONE equ 4
SA_CHECKPOINT_MANY_STAYS_MANY equ 5
SA_VERSION_CONFLICT equ 6
SA_CHECKPOINT_CONFLICT equ 7
SA_RESTART equ 8
SA_APPEND_BATCH_TWO equ 9
SA_COUNT equ 10

; rdi=parent, esi=action, rdx=child; eax=1 iff valid.
store_apply:
	push	rbx
	push	r12
	push	r13
	mov	r12, rdi
	mov	r13d, esi
	mov	rbx, rdx
	mov	rdi, rbx
	mov	rsi, r12
	call	sstate_copy
	cmp	r13d, SA_APPEND
	je	.append
	cmp	r13d, SA_COMMIT_BATCH_ONE
	je	.batch_one
	cmp	r13d, SA_COMMIT_BATCH_TWO
	je	.batch_two
	cmp	r13d, SA_CHECKPOINT_ONE
	je	.checkpoint_one
	cmp	r13d, SA_CHECKPOINT_MANY_TO_ONE
	je	.checkpoint_many_one
	cmp	r13d, SA_CHECKPOINT_MANY_STAYS_MANY
	je	.checkpoint_many_many
	cmp	r13d, SA_VERSION_CONFLICT
	je	.conflict
	cmp	r13d, SA_CHECKPOINT_CONFLICT
	je	.conflict
	cmp	r13d, SA_RESTART
	je	.restart
	cmp	r13d, SA_APPEND_BATCH_TWO
	je	.append_batch_two
	jmp	.invalid
.append:
	cmp	byte [r12 + SSTATE_PENDING_OFF], PENDING_ZERO
	jne	.append_nonzero
	mov	byte [rbx + SSTATE_PENDING_OFF], PENDING_ONE
	jmp	.append_mutants
.append_nonzero:
	mov	byte [rbx + SSTATE_PENDING_OFF], PENDING_MANY
.append_mutants:
	cmp	byte [store_mutation_mode], SMUT_DUPLICATE_EVENT_ID
	jne	.valid
	mov	byte [rbx + SSTATE_IDS_OK_OFF], 0
	jmp	.valid
.append_batch_two:
	cmp	byte [store_mutation_mode], SMUT_APPEND_BATCH_AS_ONE
	jne	.append_batch_two_valid
	mov	byte [rbx + SSTATE_PENDING_OFF], PENDING_ONE
	mov	byte [rbx + SSTATE_BATCH_OK_OFF], 0
	jmp	.valid
.append_batch_two_valid:
	mov	byte [rbx + SSTATE_PENDING_OFF], PENDING_MANY
	jmp	.valid
.batch_one:
	cmp	byte [r12 + SSTATE_PENDING_OFF], PENDING_ZERO
	je	.invalid
	jmp	.batch_mutants
.batch_two:
	cmp	byte [r12 + SSTATE_PENDING_OFF], PENDING_ZERO
	je	.invalid
	mov	byte [rbx + SSTATE_PENDING_OFF], PENDING_MANY
.batch_mutants:
	cmp	byte [store_mutation_mode], SMUT_PARTIAL_BATCH
	jne	.batch_output_cp
	mov	byte [rbx + SSTATE_BATCH_OK_OFF], 0
	jmp	.valid
.batch_output_cp:
	cmp	byte [store_mutation_mode], SMUT_OUTPUT_WITHOUT_CHECKPOINT
	jne	.batch_cp_output
	mov	byte [rbx + SSTATE_OUTPUT_CP_OK_OFF], 0
	jmp	.valid
.batch_cp_output:
	cmp	byte [store_mutation_mode], SMUT_CHECKPOINT_WITHOUT_OUTPUT
	jne	.valid
	mov	byte [rbx + SSTATE_CP_OUTPUT_OK_OFF], 0
	jmp	.valid
.checkpoint_one:
	cmp	byte [r12 + SSTATE_PENDING_OFF], PENDING_ONE
	jne	.invalid
	mov	byte [rbx + SSTATE_PENDING_OFF], PENDING_ZERO
	jmp	.valid
.checkpoint_many_one:
	cmp	byte [r12 + SSTATE_PENDING_OFF], PENDING_MANY
	jne	.invalid
	mov	byte [rbx + SSTATE_PENDING_OFF], PENDING_ONE
	jmp	.valid
.checkpoint_many_many:
	cmp	byte [r12 + SSTATE_PENDING_OFF], PENDING_MANY
	jne	.invalid
	jmp	.valid
.conflict:
	cmp	byte [store_mutation_mode], SMUT_CONFLICT_CHANGES_STATE
	jne	.valid
	mov	byte [rbx + SSTATE_CONFLICT_OK_OFF], 0
	jmp	.valid
.restart:
	cmp	byte [store_mutation_mode], SMUT_HISTORY_REWRITE
	jne	.restart_position
	mov	byte [rbx + SSTATE_APPEND_OK_OFF], 0
.restart_position:
	cmp	byte [store_mutation_mode], SMUT_POSITION_REGRESSION
	jne	.valid
	mov	byte [rbx + SSTATE_POSITION_OK_OFF], 0
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
