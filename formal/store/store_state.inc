; Finite EventStore safety abstraction. The seven *_OK bytes are ghost
; monitors for transition properties, not fields stored by SQLite.

PENDING_ZERO equ 0
PENDING_ONE equ 1
PENDING_MANY equ 2
PENDING_COUNT equ 3

SSTATE_PENDING_OFF equ 0
SSTATE_APPEND_OK_OFF equ 1
SSTATE_POSITION_OK_OFF equ 2
SSTATE_IDS_OK_OFF equ 3
SSTATE_BATCH_OK_OFF equ 4
SSTATE_OUTPUT_CP_OK_OFF equ 5
SSTATE_CP_OUTPUT_OK_OFF equ 6
SSTATE_CONFLICT_OK_OFF equ 7
SSTATE_SIZE equ 8
SSTATE_COUNT equ (PENDING_COUNT * 128)

SMUT_NONE equ 0
SMUT_HISTORY_REWRITE equ 1
SMUT_POSITION_REGRESSION equ 2
SMUT_DUPLICATE_EVENT_ID equ 3
SMUT_PARTIAL_BATCH equ 4
SMUT_OUTPUT_WITHOUT_CHECKPOINT equ 5
SMUT_CHECKPOINT_WITHOUT_OUTPUT equ 6
SMUT_CONFLICT_CHANGES_STATE equ 7
SMUT_APPEND_BATCH_AS_ONE equ 8

; rdi=state; eax=numeric id.
sstate_id:
	movzx	eax, byte [rdi + SSTATE_PENDING_OFF]
	shl	eax, 7
	xor	ecx, ecx
	mov	cl, [rdi + SSTATE_APPEND_OK_OFF]
	shl	ecx, 6
	or	eax, ecx
	mov	cl, [rdi + SSTATE_POSITION_OK_OFF]
	shl	ecx, 5
	or	eax, ecx
	mov	cl, [rdi + SSTATE_IDS_OK_OFF]
	shl	ecx, 4
	or	eax, ecx
	mov	cl, [rdi + SSTATE_BATCH_OK_OFF]
	shl	ecx, 3
	or	eax, ecx
	mov	cl, [rdi + SSTATE_OUTPUT_CP_OK_OFF]
	shl	ecx, 2
	or	eax, ecx
	mov	cl, [rdi + SSTATE_CP_OUTPUT_OK_OFF]
	shl	ecx, 1
	or	eax, ecx
	mov	cl, [rdi + SSTATE_CONFLICT_OK_OFF]
	or	eax, ecx
	ret

; edi=numeric id, rsi=destination.
sstate_decode:
	mov	eax, edi
	mov	edx, eax
	shr	edx, 7
	mov	[rsi + SSTATE_PENDING_OFF], dl
	mov	edx, eax
	shr	edx, 6
	and	dl, 1
	mov	[rsi + SSTATE_APPEND_OK_OFF], dl
	mov	edx, eax
	shr	edx, 5
	and	dl, 1
	mov	[rsi + SSTATE_POSITION_OK_OFF], dl
	mov	edx, eax
	shr	edx, 4
	and	dl, 1
	mov	[rsi + SSTATE_IDS_OK_OFF], dl
	mov	edx, eax
	shr	edx, 3
	and	dl, 1
	mov	[rsi + SSTATE_BATCH_OK_OFF], dl
	mov	edx, eax
	shr	edx, 2
	and	dl, 1
	mov	[rsi + SSTATE_OUTPUT_CP_OK_OFF], dl
	mov	edx, eax
	shr	edx, 1
	and	dl, 1
	mov	[rsi + SSTATE_CP_OUTPUT_OK_OFF], dl
	and	al, 1
	mov	[rsi + SSTATE_CONFLICT_OK_OFF], al
	ret

sstate_copy:
	mov	rax, [rsi]
	mov	[rdi], rax
	ret

sstate_is_inv:
	cmp	byte [rdi + SSTATE_PENDING_OFF], PENDING_COUNT
	jae	.no
	mov	eax, 1
	cmp	byte [rdi + SSTATE_APPEND_OK_OFF], 1
	jne	.no
	cmp	byte [rdi + SSTATE_POSITION_OK_OFF], 1
	jne	.no
	cmp	byte [rdi + SSTATE_IDS_OK_OFF], 1
	jne	.no
	cmp	byte [rdi + SSTATE_BATCH_OK_OFF], 1
	jne	.no
	cmp	byte [rdi + SSTATE_OUTPUT_CP_OK_OFF], 1
	jne	.no
	cmp	byte [rdi + SSTATE_CP_OUTPUT_OK_OFF], 1
	jne	.no
	cmp	byte [rdi + SSTATE_CONFLICT_OK_OFF], 1
	jne	.no
	ret
.no:
	xor	eax, eax
	ret

sstate_is_initial:
	cmp	byte [rdi + SSTATE_PENDING_OFF], PENDING_ZERO
	jne	.no
	jmp	sstate_is_inv
.no:
	xor	eax, eax
	ret
