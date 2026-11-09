// Card, event and route types come only from the generated file (17.4); nothing here is
// a hand-written interface. Aliases give the components short names.
import type { components } from "./api-types.gen";

type S = components["schemas"];

export type ConversationOut = S["ConversationOut"];
export type MessageOut = S["MessageOut"];
export type MessageBody = MessageOut["body"];
export type TextBody = S["TextBody"];
export type ClarifyCard = S["ClarifyCard"];
export type ShortlistCard = S["ShortlistCard"];
export type CandidateOption = S["CandidateOption"];
export type SlotOption = S["SlotOption"];
export type ActionCard = S["ActionCard"];
export type CardReminder = S["CardReminder"];
export type OutcomeCard = S["OutcomeCard"];
export type OutcomeNotice = S["OutcomeNotice"];
export type NoticeAction = S["NoticeAction"];
export type ApprovalStatus = S["ApprovalStatusOut"];
export type ClockOut = S["ClockOut"];
export type ConversationSummary = S["ConversationSummary"];
export type MetaOut = S["MetaOut"];
export type TraceOut = S["TraceOut"];
export type TraceRecord = S["TraceRecordData"];
export type CardRecord = S["CardRecordData"];
export type RunEvent = S["RunEvent"];
export type ScenariosOut = S["ScenariosOut"];
export type DevBooking = S["DevBooking"];
export type VisibleCard = S["VisibleCard"];
export type NewMessage = S["NewMessage"];
export type DecisionIn = S["DecisionIn"];
export type ActionIn = S["ActionIn"];
export type NotificationOut = S["NotificationOut"];
export type NotificationsOut = S["NotificationsOut"];
export type ProactiveLine = S["ProactiveLine"];

export type BodyOf<T extends MessageBody["type"]> = Extract<MessageBody, { type: T }>;
