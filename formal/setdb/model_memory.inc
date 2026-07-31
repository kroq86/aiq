; Pure packed-state operations. System V AMD64 calling convention.

; rdi = state
state_zero:
	push	rdi
	xor	eax, eax
	mov	ecx, STATE_SIZE
	rep	stosb
	pop	rdi
	ret

; rdi = destination, rsi = source
state_copy:
	push	rdi
	push	rsi
	mov	ecx, STATE_SIZE
	rep	movsb
	pop	rsi
	pop	rdi
	ret

; rdi = left, rsi = right; rax = 1 equal, 0 different
state_equal:
	push	rdi
	push	rsi
	mov	ecx, STATE_SIZE
	repe	cmpsb
	sete	al
	movzx	eax, al
	pop	rsi
	pop	rdi
	ret

; rdi = state, esi = 1-based position; rax = event pointer or zero
state_event_at:
	test	esi, esi
	jz	.missing
	movzx	eax, byte [rdi + STATE_HISTORY_LEN_OFF]
	cmp	esi, eax
	ja	.missing
	dec	esi
	imul	esi, EVENT_SIZE
	lea	rax, [rdi + STATE_HISTORY_OFF + rsi]
	ret
.missing:
	xor	eax, eax
	ret

; rdi=state, esi=type, edx=cause, ecx=operation, r8d=flags
; rax = appended 1 / bound exceeded 0
state_append_event:
	movzx	eax, byte [rdi + STATE_HISTORY_LEN_OFF]
	cmp	eax, MODEL_BOUND
	jae	.full
	imul	eax, EVENT_SIZE
	lea	r9, [rdi + STATE_HISTORY_OFF + rax]
	mov	[r9 + EVENT_TYPE_OFF], sil
	mov	[r9 + EVENT_CAUSE_OFF], dl
	mov	[r9 + EVENT_OPERATION_OFF], cl
	mov	[r9 + EVENT_FLAGS_OFF], r8b
	inc	byte [rdi + STATE_HISTORY_LEN_OFF]
	mov	eax, 1
	ret
.full:
	xor	eax, eax
	ret

; rdi = state; rax = number of terminal events
state_terminal_count:
	push	rbx
	push	r12
	mov	r12, rdi
	xor	ebx, ebx
	xor	r10d, r10d
.loop:
	movzx	eax, byte [r12 + STATE_HISTORY_LEN_OFF]
	cmp	r10d, eax
	jae	.done
	mov	eax, r10d
	imul	eax, EVENT_SIZE
	mov	al, [r12 + STATE_HISTORY_OFF + rax + EVENT_TYPE_OFF]
	call	event_is_terminal
	add	ebx, eax
	inc	r10d
	jmp	.loop
.done:
	mov	eax, ebx
	pop	r12
	pop	rbx
	ret

; rdi = state, esi = request position; rax = committed result count
state_result_count_for_request:
	push	rbx
	push	r12
	push	r13
	mov	r12, rdi
	mov	r13d, esi
	xor	ebx, ebx
	xor	r10d, r10d
.loop:
	movzx	eax, byte [r12 + STATE_HISTORY_LEN_OFF]
	cmp	r10d, eax
	jae	.done
	mov	eax, r10d
	imul	eax, EVENT_SIZE
	lea	r11, [r12 + STATE_HISTORY_OFF + rax]
	mov	al, [r11 + EVENT_TYPE_OFF]
	call	event_is_result
	test	eax, eax
	jz	.next
	movzx	eax, byte [r11 + EVENT_CAUSE_OFF]
	cmp	eax, r13d
	jne	.next
	inc	ebx
.next:
	inc	r10d
	jmp	.loop
.done:
	mov	eax, ebx
	pop	r13
	pop	r12
	pop	rbx
	ret
