import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";

const sendMessageTool = defineTool({
	name: "send_message",
	label: "Send Message",
	description:
		"Send a message to the user or another configured Alice principal. Returning text alone does not send it.",
	promptSnippet:
		"Use send_message to reply. recipient='self' replies on the current channel.",
	promptGuidelines: [
		"Use send_message for inbound replies.",
		"Use recipient='self' or recipient='reply' to answer on the current channel.",
		"Do not rely on final assistant text being delivered to the user.",
	],
	parameters: Type.Object({
		recipient: Type.String({
			description:
				"'self' or 'reply' for the current channel, a known principal id/display name, or an E.164 number.",
		}),
		message: Type.String({ description: "Text to deliver." }),
		attachments: Type.Optional(
			Type.Array(Type.String(), {
				description: "Optional filesystem paths to send as attachments.",
			}),
		),
	}),

	async execute(_toolCallId, params) {
		return {
			content: [
				{
					type: "text",
					text:
						`queued message to ${params.recipient} ` +
						`(${params.message.length} chars)`,
				},
			],
		};
	},
});

export default function aliceSendMessageExtension(pi) {
	pi.registerTool(sendMessageTool);
}
