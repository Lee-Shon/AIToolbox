// Compile against the exact patched LocalAI/llama sources; no model or GPU.
#define main localai_backend_main
#include "grpc-server.cpp"
#undef main

void require(bool value, const char * detail) {
    if (!value) throw std::runtime_error(detail);
}

int main() {
    server_queue tasks;
    server_response results;
    int polls = 0;
    tasks.on_new_task([&](server_task && task, bool) {
        if (task.type == SERVER_TASK_TYPE_SLOT_GET) {
            auto reply = std::make_unique<server_task_result_slots>();
            reply->id = task.id;
            reply->slots_data = json::array({
                {{"id_task", 42}, {"is_processing", ++polls < 3}},
                {{"id_task", 99}, {"is_processing", true}}});
            results.send(std::move(reply));
        }
        return true;
    });
    tasks.on_update_slots([]() {});
    std::thread queue_thread([&]() { tasks.start_loop(); });
    const bool stopped = BackendServiceImpl::wait_native_targets(
        [&]() { return server_response_reader(tasks, results, 1); }, {42},
        std::chrono::steady_clock::now() + std::chrono::seconds(3));
    tasks.terminate();
    queue_thread.join();
    require(stopped && polls == 3,
            "repeated cancellation polls must use fresh readers and ignore other targets");

    server_task_result_cmpl_final final{};
    final.n_decoded = 16;
    final.n_prompt_tokens = 28;
    backend::Reply streamed;
    BackendServiceImpl::native_final_usage(streamed, &final);
    require(streamed.tokens() == 16 && streamed.prompt_tokens() == 28,
            "streaming usage must come from the native final result");

    backend::PredictOptions first;
    first.set_tokens(32);
    first.set_seed(7);
    first.add_audios("first-staged-path");
    auto * message = first.add_messages();
    message->set_role("user");
    message->set_content("first question");
    message = first.add_messages();
    message->set_role("assistant");
    message->set_content("first reply");
    message = first.add_messages();
    message->set_role("user");
    message->set_content("second question");
    (*first.mutable_metadata())["aitoolbox_phase"] = "prepare";
    (*first.mutable_metadata())["aitoolbox_input_digest"] = "original-bytes";
    (*first.mutable_metadata())["chat_template_kwargs"] = "{\"z\":1,\"a\":2}";
    auto second = first;
    second.set_correlationid("new-transport-id");
    second.set_audios(0, "second-staged-path");
    (*second.mutable_metadata())["aitoolbox_phase"] = "execute";
    (*second.mutable_metadata())["chat_template_kwargs"] = "{\"a\":2,\"z\":1}";
    const auto original = BackendServiceImpl::preparation_fingerprint(&first);
    require(original == BackendServiceImpl::preparation_fingerprint(&second),
            "transport/map order must not change prepared input identity");
    second.set_tokens(31);
    require(original != BackendServiceImpl::preparation_fingerprint(&second),
            "changed output limit must be rejected");
    second = first;
    (*second.mutable_metadata())["aitoolbox_input_digest"] = "other-original-bytes";
    require(original != BackendServiceImpl::preparation_fingerprint(&second),
            "changed original media digest must be rejected");

    (*first.mutable_metadata())["aitoolbox_media_layout"] =
        "[[{\"type\":\"text\",\"text\":\"first question\"},{\"type\":\"audio\"}],null,null]";
    json messages = json::array();
    for (const auto & item : first.messages())
        messages.push_back({{"role", item.role()}, {"content", item.content()}});
    BackendServiceImpl::restore_media_layout(messages, &first);
    require(messages[0]["content"][1]["type"].get<std::string>() == "input_audio",
            "media must remain in its original turn");
    require(messages[2]["content"].get<std::string>() == "second question",
            "later user turn must retain its original content");
    first.add_audios("unmapped-extra-audio");
    bool rejected = false;
    try { BackendServiceImpl::restore_media_layout(messages, &first); }
    catch (const std::runtime_error &) { rejected = true; }
    require(rejected, "unmapped media cannot be silently dropped");
    std::cout << "native fingerprint, media placement and mismatch checks passed\n";
}
