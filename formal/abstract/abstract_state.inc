; Finite local ModelLoopModel state.

CLASS_LOW equ 0
CLASS_BEFORE equ 1
CLASS_AT equ 2
CLASS_COUNT equ 3

PHASE_IDLE equ 0
PHASE_MODEL_PENDING equ 1
PHASE_TOOL_PENDING equ 2
PHASE_LIMIT equ 3
PHASE_COMPLETED equ 4
PHASE_FAILED equ 5
PHASE_COUNT equ 6

ASTATE_PHASE_OFF equ 0
ASTATE_MODEL_CLASS_OFF equ 1
ASTATE_TOOL_CLASS_OFF equ 2
ASTATE_SIZE equ 3
ASTATE_COUNT equ (PHASE_COUNT * CLASS_COUNT * CLASS_COUNT)

AMUT_NONE equ 0
AMUT_BEFORE_STAYS equ 1
AMUT_AT_ALLOWS equ 2
AMUT_TOOL_UPDATES_MODEL equ 3
AMUT_MODEL_UPDATES_TOOL equ 4
AMUT_TOOL_RESET equ 5

; astate pointer -> numeric id
astate_id:
	movzx	eax, byte [rdi + ASTATE_PHASE_OFF]
	imul	eax, CLASS_COUNT * CLASS_COUNT
	movzx	ecx, byte [rdi + ASTATE_MODEL_CLASS_OFF]
	imul	ecx, CLASS_COUNT
	add	eax, ecx
	movzx	ecx, byte [rdi + ASTATE_TOOL_CLASS_OFF]
	add	eax, ecx
	ret

; edi=numeric id, rsi=destination
astate_decode:
	mov	eax, edi
	xor	edx, edx
	mov	ecx, CLASS_COUNT * CLASS_COUNT
	div	ecx
	mov	[rsi + ASTATE_PHASE_OFF], al
	mov	eax, edx
	xor	edx, edx
	mov	ecx, CLASS_COUNT
	div	ecx
	mov	[rsi + ASTATE_MODEL_CLASS_OFF], al
	mov	[rsi + ASTATE_TOOL_CLASS_OFF], dl
	ret

; rdi=state; eax=1 iff state belongs to strengthened invariant.
astate_is_inv:
	movzx	eax, byte [rdi + ASTATE_PHASE_OFF]
	cmp	eax, PHASE_COUNT
	jae	.no
	cmp	eax, PHASE_IDLE
	jne	.yes
	cmp	byte [rdi + ASTATE_MODEL_CLASS_OFF], CLASS_AT
	je	.no
	cmp	byte [rdi + ASTATE_TOOL_CLASS_OFF], CLASS_AT
	je	.no
.yes:
	mov	eax, 1
	ret
.no:
	xor	eax, eax
	ret

; rdi=state; eax=1 iff abstract initial.
astate_is_initial:
	cmp	byte [rdi + ASTATE_PHASE_OFF], PHASE_IDLE
	jne	.no
	cmp	byte [rdi + ASTATE_MODEL_CLASS_OFF], CLASS_AT
	je	.no
	cmp	byte [rdi + ASTATE_TOOL_CLASS_OFF], CLASS_AT
	je	.no
	mov	eax, 1
	ret
.no:
	xor	eax, eax
	ret

astate_copy:
	mov	ax, [rsi]
	mov	[rdi], ax
	mov	al, [rsi + 2]
	mov	[rdi + 2], al
	ret

astate_equal:
	mov	ax, [rdi]
	cmp	ax, [rsi]
	jne	.no
	mov	al, [rdi + 2]
	cmp	al, [rsi + 2]
	jne	.no
	mov	eax, 1
	ret
.no:
	xor	eax, eax
	ret
