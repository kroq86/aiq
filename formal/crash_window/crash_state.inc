; Finite operational abstraction for one durable external operation.

DP_NONE equ 0
DP_REQUESTED equ 1
DP_RESULT equ 2
DP_FAILED equ 3
DP_COUNT equ 4

OP_IDLE equ 0
OP_INVOKED equ 1
OP_COUNT equ 2

OID_NONE equ 0
OID_ORIGINAL equ 1
OID_OTHER equ 2
OID_COUNT equ 3

COUNT_ZERO equ 0
COUNT_ONE equ 1
COUNT_MANY equ 2
COUNT_COUNT equ 3

CSTATE_DURABLE_OFF equ 0
CSTATE_OPERATIONAL_OFF equ 1
CSTATE_OPERATION_ID_OFF equ 2
CSTATE_PHYSICAL_OFF equ 3
CSTATE_COMMITTED_OFF equ 4
CSTATE_SIZE equ 5
CSTATE_COUNT equ (DP_COUNT * OP_COUNT * OID_COUNT * COUNT_COUNT * COUNT_COUNT)

CMUT_NONE equ 0
CMUT_DUPLICATE_COMMIT equ 1
CMUT_RETRY_NEW_OPERATION equ 2

cstate_id:
	movzx	eax, byte [rdi + CSTATE_DURABLE_OFF]
	imul	eax, OP_COUNT
	movzx	ecx, byte [rdi + CSTATE_OPERATIONAL_OFF]
	add	eax, ecx
	imul	eax, OID_COUNT
	movzx	ecx, byte [rdi + CSTATE_OPERATION_ID_OFF]
	add	eax, ecx
	imul	eax, COUNT_COUNT
	movzx	ecx, byte [rdi + CSTATE_PHYSICAL_OFF]
	add	eax, ecx
	imul	eax, COUNT_COUNT
	movzx	ecx, byte [rdi + CSTATE_COMMITTED_OFF]
	add	eax, ecx
	ret

cstate_decode:
	mov	eax, edi
	xor	edx, edx
	mov	ecx, COUNT_COUNT
	div	ecx
	mov	[rsi + CSTATE_COMMITTED_OFF], dl
	xor	edx, edx
	div	ecx
	mov	[rsi + CSTATE_PHYSICAL_OFF], dl
	xor	edx, edx
	mov	ecx, OID_COUNT
	div	ecx
	mov	[rsi + CSTATE_OPERATION_ID_OFF], dl
	xor	edx, edx
	mov	ecx, OP_COUNT
	div	ecx
	mov	[rsi + CSTATE_OPERATIONAL_OFF], dl
	mov	[rsi + CSTATE_DURABLE_OFF], al
	ret

cstate_copy:
	mov	eax, [rsi]
	mov	[rdi], eax
	mov	al, [rsi + 4]
	mov	[rdi + 4], al
	ret

; Strengthened invariant. This contains all normal initial states and is closed
; under normal Next. It encodes stable operation identity and at-most-one commit.
cstate_is_inv:
	cmp	byte [rdi + CSTATE_COMMITTED_OFF], COUNT_MANY
	je	.no
	cmp	byte [rdi + CSTATE_OPERATION_ID_OFF], OID_OTHER
	je	.no
	movzx	eax, byte [rdi + CSTATE_DURABLE_OFF]
	cmp	eax, DP_NONE
	je	.none
	cmp	byte [rdi + CSTATE_OPERATION_ID_OFF], OID_ORIGINAL
	jne	.no
	cmp	eax, DP_REQUESTED
	je	.requested
	cmp	byte [rdi + CSTATE_OPERATIONAL_OFF], OP_IDLE
	jne	.no
	cmp	eax, DP_RESULT
	je	.result
	cmp	eax, DP_FAILED
	je	.failed
	jmp	.no
.none:
	cmp	byte [rdi + CSTATE_OPERATIONAL_OFF], OP_IDLE
	jne	.no
	cmp	byte [rdi + CSTATE_OPERATION_ID_OFF], OID_NONE
	jne	.no
	cmp	byte [rdi + CSTATE_PHYSICAL_OFF], COUNT_ZERO
	jne	.no
	cmp	byte [rdi + CSTATE_COMMITTED_OFF], COUNT_ZERO
	jne	.no
	jmp	.yes
.requested:
	cmp	byte [rdi + CSTATE_COMMITTED_OFF], COUNT_ZERO
	jne	.no
	cmp	byte [rdi + CSTATE_OPERATIONAL_OFF], OP_INVOKED
	jne	.yes
	cmp	byte [rdi + CSTATE_PHYSICAL_OFF], COUNT_ZERO
	je	.no
	jmp	.yes
.result:
	cmp	byte [rdi + CSTATE_COMMITTED_OFF], COUNT_ONE
	jne	.no
	cmp	byte [rdi + CSTATE_PHYSICAL_OFF], COUNT_ZERO
	je	.no
	jmp	.yes
.failed:
	cmp	byte [rdi + CSTATE_COMMITTED_OFF], COUNT_ZERO
	jne	.no
.yes:
	mov	eax, 1
	ret
.no:
	xor	eax, eax
	ret

cstate_is_initial:
	cmp	byte [rdi + CSTATE_DURABLE_OFF], DP_NONE
	jne	.no
	cmp	byte [rdi + CSTATE_OPERATIONAL_OFF], OP_IDLE
	jne	.no
	cmp	byte [rdi + CSTATE_OPERATION_ID_OFF], OID_NONE
	jne	.no
	cmp	byte [rdi + CSTATE_PHYSICAL_OFF], COUNT_ZERO
	jne	.no
	cmp	byte [rdi + CSTATE_COMMITTED_OFF], COUNT_ZERO
	jne	.no
	mov	eax, 1
	ret
.no:
	xor	eax, eax
	ret
