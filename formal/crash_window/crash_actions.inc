CA_COMMIT_REQUEST equ 0
CA_INVOKE equ 1
CA_CRASH equ 2
CA_RESTART equ 3
CA_COMMIT_RESULT equ 4
CA_FORCE_FAIL equ 5
CA_COUNT equ 6

; Saturating increment of a 0|1|many counter byte.
; rdi points at the counter.
crash_increment:
	cmp	byte [rdi], COUNT_MANY
	je	.done
	inc	byte [rdi]
.done:
	ret

; rdi=parent, esi=action, rdx=child; eax=1 iff valid.
crash_apply:
	push	rbx
	push	r12
	push	r13
	mov	r12, rdi
	mov	r13d, esi
	mov	rbx, rdx
	mov	rdi, rbx
	mov	rsi, r12
	call	cstate_copy
	cmp	r13d, CA_COMMIT_REQUEST
	je	.commit_request
	cmp	r13d, CA_INVOKE
	je	.invoke
	cmp	r13d, CA_CRASH
	je	.crash
	cmp	r13d, CA_RESTART
	je	.restart
	cmp	r13d, CA_COMMIT_RESULT
	je	.commit_result
	cmp	r13d, CA_FORCE_FAIL
	je	.force_fail
	jmp	.invalid
.commit_request:
	cmp	byte [r12 + CSTATE_DURABLE_OFF], DP_NONE
	jne	.invalid
	mov	byte [rbx + CSTATE_DURABLE_OFF], DP_REQUESTED
	mov	byte [rbx + CSTATE_OPERATION_ID_OFF], OID_ORIGINAL
	jmp	.valid
.invoke:
	cmp	byte [r12 + CSTATE_DURABLE_OFF], DP_REQUESTED
	jne	.invalid
	cmp	byte [r12 + CSTATE_OPERATIONAL_OFF], OP_IDLE
	jne	.invalid
	cmp	byte [crash_mutation_mode], CMUT_RETRY_NEW_OPERATION
	jne	.invoke_id_done
	cmp	byte [r12 + CSTATE_PHYSICAL_OFF], COUNT_ZERO
	je	.invoke_id_done
	mov	byte [rbx + CSTATE_OPERATION_ID_OFF], OID_OTHER
.invoke_id_done:
	mov	byte [rbx + CSTATE_OPERATIONAL_OFF], OP_INVOKED
	lea	rdi, [rbx + CSTATE_PHYSICAL_OFF]
	call	crash_increment
	jmp	.valid
.crash:
	cmp	byte [r12 + CSTATE_DURABLE_OFF], DP_REQUESTED
	jne	.invalid
	cmp	byte [r12 + CSTATE_OPERATIONAL_OFF], OP_INVOKED
	jne	.invalid
	mov	byte [rbx + CSTATE_OPERATIONAL_OFF], OP_IDLE
	jmp	.valid
.restart:
	cmp	byte [r12 + CSTATE_DURABLE_OFF], DP_REQUESTED
	jne	.invalid
	mov	byte [rbx + CSTATE_OPERATIONAL_OFF], OP_IDLE
	jmp	.valid
.commit_result:
	cmp	byte [r12 + CSTATE_DURABLE_OFF], DP_REQUESTED
	je	.commit_requested
	cmp	byte [crash_mutation_mode], CMUT_DUPLICATE_COMMIT
	jne	.invalid
	cmp	byte [r12 + CSTATE_DURABLE_OFF], DP_RESULT
	jne	.invalid
	lea	rdi, [rbx + CSTATE_COMMITTED_OFF]
	call	crash_increment
	jmp	.valid
.commit_requested:
	cmp	byte [r12 + CSTATE_OPERATIONAL_OFF], OP_INVOKED
	jne	.invalid
	mov	byte [rbx + CSTATE_DURABLE_OFF], DP_RESULT
	mov	byte [rbx + CSTATE_OPERATIONAL_OFF], OP_IDLE
	mov	byte [rbx + CSTATE_COMMITTED_OFF], COUNT_ONE
	jmp	.valid
.force_fail:
	cmp	byte [r12 + CSTATE_DURABLE_OFF], DP_REQUESTED
	jne	.invalid
	mov	byte [rbx + CSTATE_DURABLE_OFF], DP_FAILED
	mov	byte [rbx + CSTATE_OPERATIONAL_OFF], OP_IDLE
	jmp	.valid
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
