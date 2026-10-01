// Included inside BackendServiceImpl after the native preparation helper.
// Owns only prepared inputs; the Windows SharedContext remains the quota pool.
struct prepared_input {
    json data;
    std::vector<server_tokens> inputs;
    std::string fingerprint;
    size_t input_tokens = 0;
    int choices = 1;
    int executions = 0;
    int output_limit = 0;
    bool cancelled = false;
    json native_executions = json::array();
    std::map<int, grpc::ServerContext *> contexts;
};
std::mutex preparation_mutex;
std::map<std::string, std::shared_ptr<prepared_input>> prepared_inputs;

json resource_profile() {
    auto * context = ctx_server.get_llama_context();
    size_t gpu = 0;
    json buffers = json::array();
    for (const auto & pair : llama_get_memory_breakdown(context)) {
        const auto type = pair.first;
        auto device = ggml_backend_buft_get_device(type);
        if (device && ggml_backend_dev_type(device) == GGML_BACKEND_DEVICE_TYPE_GPU) {
            gpu += pair.second.total();
            buffers.push_back({{"type", ggml_backend_buft_name(type)},
                {"model", pair.second.model}, {"context", pair.second.context}, {"compute", pair.second.compute}});
        }
    }
    char precision[64] = {};
    llama_model_meta_val_str(llama_get_model(context), "general.file_type", precision, sizeof(precision));
    const size_t headroom = 1024ULL * 1024 * 1024;
    return {{"resident_gpu_bytes", gpu + headroom}, {"load_gpu_bytes", gpu + headroom},
        {"driver_workspace_headroom_bytes", headroom}, {"native_buffers", buffers},
        {"total_context_tokens", llama_n_ctx(context)}, {"precision", precision},
        {"adapter_sha256", AITOOLBOX_ADAPTER_SHA256}, {"source", "llama_native_full_context_buffers_with_driver_headroom"}};
}

struct native_scope {
    std::function<void()> finish;
    ~native_scope() { if (finish) finish(); }
};

void submit_native(const backend::PredictOptions * request, grpc::ServerContext * context,
                   std::vector<server_task> && tasks, native_scope & scope,
                   server_response_reader & reader) {
    std::lock_guard<std::mutex> guard(preparation_mutex);
    const auto key = preparation_metadata(request, "aitoolbox_request_id");
    auto found = prepared_inputs.find(key);
    if (found == prepared_inputs.end()) { reader.post_tasks(std::move(tasks)); return; }
    auto entry = found->second;
    if (entry->cancelled) throw std::runtime_error("prepared_input_cancelled");
    std::vector<int> ids;
    for (const auto & task : tasks) {
        ids.push_back(task.id);
        entry->contexts[task.id] = context;
        entry->native_executions.push_back({{"id_task", task.id}, {"finished", false},
            {"output_tokens", 0}, {"finish_reason", nullptr}});
    }
    scope.finish = [this, entry, ids]() {
        std::lock_guard<std::mutex> guard(preparation_mutex);
        for (int id : ids) entry->contexts.erase(id);
    };
    // Publish native task IDs and enqueue atomically with respect to cancel.
    reader.post_tasks(std::move(tasks));
}

void observe_native(const backend::PredictOptions * request, server_task_result * result) {
    std::lock_guard<std::mutex> guard(preparation_mutex);
    auto found = prepared_inputs.find(preparation_metadata(request, "aitoolbox_request_id"));
    if (found == prepared_inputs.end()) return;
    for (auto & item : found->second->native_executions) {
        if (item["id_task"] != result->id) continue;
        if (auto * final = dynamic_cast<server_task_result_cmpl_final *>(result)) {
            item["output_tokens"] = final->n_decoded;
            item["finish_reason"] = final->stop == STOP_TYPE_LIMIT ? "length" : "stop";
            item["finished"] = true;
        }
        if (result->is_error()) {
            item["error"] = result->to_json();
            item["finished"] = true;
        }
    }
}

json native_status(const std::shared_ptr<prepared_input> & entry, bool confirmed) {
    bool hit = false, completed = entry->native_executions.size() == static_cast<size_t>(entry->choices);
    for (const auto & item : entry->native_executions) {
        completed = completed && item.value("finished", false);
        hit = hit || (item.value("finish_reason", json()) == "length" &&
                      item.value("output_tokens", 0) >= entry->output_limit);
    }
    return {{"execution_started", !entry->native_executions.empty()},
            {"native_context_tokens", llama_n_ctx(ctx_server.get_llama_context())},
            {"native_slot_context_tokens", ctx_server.get_meta().slot_n_ctx},
            {"native_executions", entry->native_executions}, {"cancel_requested", entry->cancelled},
            {"stop_confirmed", confirmed && (completed || entry->cancelled)},
            {"confirmed_output_hit", hit}, {"basis", "llama_native_final_or_target_cancel_slot_barrier"}};
}

template <typename ReaderFactory>
static bool wait_native_targets(ReaderFactory reader_factory, const std::vector<int> & ids,
                                std::chrono::steady_clock::time_point deadline) {
    while (std::chrono::steady_clock::now() < deadline) {
        // A native response reader owns exactly one submission. Reusing it for
        // a second slot poll aborts the entire shared backend (GGML_ASSERT).
        auto reader = reader_factory();
        server_task probe(SERVER_TASK_TYPE_SLOT_GET);
        probe.id = reader.get_new_id();
        reader.post_task(std::move(probe));
        auto result = reader.next([&]() { return std::chrono::steady_clock::now() >= deadline; });
        auto * slots = dynamic_cast<server_task_result_slots *>(result.get());
        if (!slots) return false;
        bool active = false;
        for (const auto & slot : slots->slots_data) {
            if (slot.value("is_processing", false) &&
                std::find(ids.begin(), ids.end(), slot.value("id_task", -1)) != ids.end()) active = true;
        }
        // CANCEL is handled by the server queue outside decode and removes
        // pending targets. This ordered slot observation confirms target exit;
        // do not touch the shared llama context from the RPC thread while an
        // unrelated slot may be decoding.
        if (!active) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    return false;
}

bool execution_control(const backend::PredictOptions * request, backend::Reply & reply) {
    const auto phase = preparation_metadata(request, "aitoolbox_phase");
    if (phase != "status" && phase != "cancel") return false;
    std::shared_ptr<prepared_input> entry;
    std::vector<int> ids;
    {
        std::lock_guard<std::mutex> guard(preparation_mutex);
        auto found = prepared_inputs.find(preparation_metadata(request, "aitoolbox_request_id"));
        if (found == prepared_inputs.end()) throw std::runtime_error("prepared_input_missing");
        entry = found->second;
        if (phase == "cancel") {
            const auto finished = native_status(entry, true);
            if (finished.value("stop_confirmed", false) && !entry->cancelled) {
                auto preserved = finished;
                preserved["already_finished"] = true;
                reply.set_message(preserved.dump());
                return true;
            }
            entry->cancelled = true;
            for (const auto & item : entry->native_executions) ids.push_back(item["id_task"].get<int>());
            for (auto & item : entry->contexts) item.second->TryCancel();
        }
    }
    bool confirmed = phase == "status";
    if (phase == "cancel") {
        auto reader = ctx_server.get_response_reader();
        std::vector<server_task> cancellations;
        for (int id : ids) {
            server_task task(SERVER_TASK_TYPE_CANCEL);
            task.id_target = id;
            cancellations.push_back(std::move(task));
        }
        reader.queue_tasks.post(std::move(cancellations), true);
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
        confirmed = wait_native_targets([&]() { return ctx_server.get_response_reader(); }, ids, deadline);
    }
    std::lock_guard<std::mutex> guard(preparation_mutex);
    if (confirmed && phase == "cancel") for (auto & item : entry->native_executions)
        if (!item.value("finished", false)) {
            item["finished"] = true; item["finish_reason"] = "abort";
        }
    reply.set_message(native_status(entry, confirmed).dump());
    return true;
}

// The native streaming JSON omits usage unless requested separately. The final
// task result always carries the actual counts; never substitute JSON defaults.
static void native_final_usage(backend::Reply & reply, server_task_result * result) {
    if (auto * final = dynamic_cast<server_task_result_cmpl_final *>(result)) {
        reply.set_tokens(final->n_decoded);
        reply.set_prompt_tokens(final->n_prompt_tokens);
    }
}

static std::string preparation_metadata(const backend::PredictOptions * request,
                                        const std::string & key,
                                        const std::string & fallback = "") {
    auto found = request->metadata().find(key);
    return found == request->metadata().end() ? fallback : found->second;
}

static std::string preparation_fingerprint(const backend::PredictOptions * request) {
    backend::PredictOptions value = *request;
    value.clear_correlationid();
    value.mutable_metadata()->erase("aitoolbox_phase");
    (*value.mutable_metadata())["chat_template_kwargs"] = nlohmann::json::parse(
        preparation_metadata(request, "chat_template_kwargs", "{}")).dump();
    if (!preparation_metadata(request, "aitoolbox_input_digest").empty()) {
        value.clear_images();
        value.clear_audios();
        value.clear_videos();
    }
    std::string encoded;
    auto status = google::protobuf::util::MessageToJsonString(value, &encoded);
    if (!status.ok()) throw std::runtime_error("invalid_preparation_request");
    // JSON canonicalization removes protobuf map iteration order differences.
    return nlohmann::json::parse(encoded).dump();
}

// Rebuild the message's original media positions before the native template.
// The flat LocalAI RPC otherwise attaches every media item to the last user.
static void restore_media_layout(json & messages, const backend::PredictOptions * request) {
    auto encoded = preparation_metadata(request, "aitoolbox_media_layout");
    if (encoded.empty()) return;
    auto layout = json::parse(encoded);
    if (!layout.is_array() || layout.size() != messages.size())
        throw std::runtime_error("invalid_media_layout");
    int image = 0, audio = 0, video = 0;
    for (size_t i = 0; i < layout.size(); ++i) {
        if (layout[i].is_null()) continue;
        json parts = json::array();
        for (const auto & part : layout[i]) {
            const auto kind = part.at("type").get<std::string>();
            if (kind == "text") parts.push_back(part);
            else if (kind == "image" && image < request->images_size())
                parts.push_back({{"type", "image_url"}, {"image_url", {
                    {"url", "data:image/jpeg;base64," + request->images(image++)}}}});
            else if (kind == "audio" && audio < request->audios_size())
                parts.push_back({{"type", "input_audio"}, {"input_audio", {
                    {"data", request->audios(audio++)}, {"format", "wav"}}}});
            else if (kind == "video" && video < request->videos_size())
                parts.push_back({{"type", "input_video"}, {"input_video", {
                    {"data", request->videos(video++)}}}});
            else throw std::runtime_error("invalid_media_layout");
        }
        messages[i]["content"] = parts;
    }
    if (image != request->images_size() || audio != request->audios_size() ||
        video != request->videos_size()) throw std::runtime_error("media_layout_count_mismatch");
}

bool resolve_prepared(const backend::PredictOptions * request, bool streaming,
                      json & data, std::vector<server_tokens> & inputs, backend::Reply & reply) {
    if (execution_control(request, reply)) return true;
    std::lock_guard<std::mutex> guard(preparation_mutex);
    const auto phase = preparation_metadata(request, "aitoolbox_phase");
    const auto key = preparation_metadata(request, "aitoolbox_request_id");
    if (!phase.empty() && (key.empty() ||
        (phase != "prepare" && phase != "execute" && phase != "discard")))
        throw std::runtime_error("invalid_preparation_operation");
    if (phase == "discard") {
        prepared_inputs.erase(key);
        reply.set_message("{\"aitoolbox_discarded\":1}");
        return true;
    }
    auto found = prepared_inputs.find(key);
    auto entry = phase.empty() || found == prepared_inputs.end() ? nullptr : found->second;
    const auto fingerprint = preparation_fingerprint(request);
    if (entry && entry->fingerprint != fingerprint) {
        const auto before = nlohmann::json::parse(entry->fingerprint);
        const auto after = nlohmann::json::parse(fingerprint);
        std::string changed;
        auto keys = before;
        keys.update(after);
        for (auto item : keys.items()) {
            auto key = item.key();
            if (before.value(key, nlohmann::json()) != after.value(key, nlohmann::json()))
                changed += (changed.empty() ? "" : ",") + key;
        }
        throw std::runtime_error("prepared_input_changed:" + changed);
    }
    if (phase == "execute" && !entry)
        throw std::runtime_error("prepared_input_missing");
    if (!entry) {
        entry = std::make_shared<prepared_input>();
        entry->data = parse_options(true, request, params_base, ctx_server.get_llama_context());
        entry->data["stream"] = false;
        entry->inputs = prepare_native_input(request, entry->data);
        if (entry->inputs.size() != 1)
            throw std::runtime_error("native_context_dimension_unsupported");
        entry->input_tokens = entry->inputs[0].size();
        auto quota = std::stoll(preparation_metadata(request, "aitoolbox_context_tokens",
                               std::to_string(llama_n_ctx(ctx_server.get_llama_context()))));
        if (request->tokens() <= 0 || quota <= 0)
            throw std::runtime_error("context_and_output_limits_required");
        if (quota > llama_n_ctx(ctx_server.get_llama_context()))
            throw std::runtime_error("request_exceeds_instance_capacity");
        if (entry->input_tokens + request->tokens() > static_cast<size_t>(quota))
            throw std::runtime_error("requested_context_insufficient");
        entry->choices = std::stoi(preparation_metadata(request, "aitoolbox_choices", "1"));
        entry->output_limit = request->tokens();
        if (entry->choices <= 0) throw std::runtime_error("invalid_response_choices");
        entry->fingerprint = fingerprint;
        if (phase == "prepare") prepared_inputs.emplace(key, entry);
    }
    if (phase == "prepare") {
        reply.set_message(json({{"aitoolbox_prepared", 1}, {"input_tokens", entry->input_tokens},
            {"resource_profile", resource_profile()},
            {"native_template_kwargs", json::parse(preparation_metadata(
                request, "chat_template_kwargs", "{}"))}}).dump());
        return true;
    }
    if (entry->executions >= entry->choices)
        throw std::runtime_error("prepared_input_already_executed");
    if (entry->cancelled) throw std::runtime_error("prepared_input_cancelled");
    ++entry->executions;
    data = entry->data;
    data["stream"] = streaming;
    // Clone native tokens/chunks for task ownership; no template or processor rerun.
    for (const auto & prepared : entry->inputs) inputs.push_back(prepared.clone());
    return false;
}
