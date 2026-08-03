; AIQ 0.2 bounded formal model -- FASM fact generator.
;
; The executable writes setdb's durable SADD/RADD wire format to stdout.
; MODEL_MUTATION selects one local broken rule. The common invariant
; checker derives violations from packed history. No Python participates.

format ELF64 executable 3
entry start

include 'model_state.inc'
include 'model_memory.inc'
include 'model_actions.inc'
include 'model_bfs.inc'
include 'model_invariants.inc'
include 'model_emit.inc'

segment readable executable
start:
	call	model_explore
	cmp	dword [model_error], 0
	jne	.failed
	call	model_check_all
	call	model_emit_graph
	mov	rax, 02000001h          ; macOS exit
	xor	rdi, rdi
	syscall
.failed:
	mov	rax, 02000001h
	mov	rdi, 1
	syscall

segment readable writeable
emit_state db 'SADD States s'
emit_state_id db '00000000',10
emit_state_end:
model_hex_digits db '0123456789abcdef'
emit_encoding db '# ENC s'
emit_encoding_state db '00000000'
db ' x'
emit_encoding_prefix_end:
emit_newline db 10
emit_encoding_hex:
times STATE_SIZE * 2 db '0'
emit_encoding_end:
emit_beta db 'RADD Beta s'
emit_beta_concrete db '00000000'
db ' a'
emit_beta_abstract db '00000000',10
emit_beta_end:
emit_initial db 'SADD Initial s00000000',10
emit_initial_end:
emit_transition db 'RADD Transition s'
emit_transition_parent db '00000000'
db ' s'
emit_transition_child db '00000000',10
emit_transition_end:
emit_parent db 'RADD Parent s'
emit_parent_child db '00000000'
db ' s'
emit_parent_parent db '00000000',10
emit_parent_end:
emit_action db 'RADD ParentAction s'
emit_action_child db '00000000'
db ' a'
emit_action_id db '00000000',10
emit_action_end:
emit_violation_parent db 'RADD ViolationParent s'
emit_violation_child db '00000000'
db ' s'
emit_violation_parent_id db '00000000',10
emit_violation_parent_end:
emit_violation_action db 'RADD ViolationAction s'
emit_violation_action_child db '00000000'
db ' a'
emit_violation_action_id db '00000000',10
emit_violation_action_end:
emit_bad_history db 'SADD BadHistoryAppendOnly s'
emit_bad_history_id db '00000000',10
emit_bad_history_end:
emit_bad_checkpoint db 'SADD BadCheckpointMonotonic s'
emit_bad_checkpoint_id db '00000000',10
emit_bad_checkpoint_end:
emit_bad_request db 'SADD BadResultHasRequest s'
emit_bad_request_id db '00000000',10
emit_bad_request_end:
emit_bad_operation db 'SADD BadResultOperationMatchesRequest s'
emit_bad_operation_id db '00000000',10
emit_bad_operation_end:
emit_bad_result db 'SADD BadAtMostOneResultPerRequest s'
emit_bad_result_id db '00000000',10
emit_bad_result_end:
emit_bad_terminal db 'SADD BadAtMostOneTerminal s'
emit_bad_terminal_id db '00000000',10
emit_bad_terminal_end:
emit_bad_absorbing db 'SADD BadTerminalIsAbsorbing s'
emit_bad_absorbing_id db '00000000',10
emit_bad_absorbing_end:
emit_bad_definition db 'SADD BadDefinitionResourceConsistent s'
emit_bad_definition_id db '00000000',10
emit_bad_definition_end:

; Mach-O zero-fill storage must remain after every initialized template.
model_state_count rd 1
model_transition_count rd 1
model_error rd 1
model_scratch rb STATE_SIZE
model_states rb MODEL_MAX_STATES * STATE_SIZE
model_parent rd MODEL_MAX_STATES
model_parent_action rb MODEL_MAX_STATES
model_expandable rb MODEL_MAX_STATES
model_transition_parent rd MODEL_MAX_TRANSITIONS
model_transition_child rd MODEL_MAX_TRANSITIONS
model_transition_action rb MODEL_MAX_TRANSITIONS
model_bad_mask rb MODEL_MAX_STATES
model_violation_parent rd MODEL_MAX_STATES
model_violation_action rb MODEL_MAX_STATES
