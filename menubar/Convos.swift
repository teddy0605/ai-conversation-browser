// Convos: menu bar controller for the AI Conversation Browser (app.py).
// Start/stop/restart the local server, trigger a reindex, open the page.
// Build and install with ./build.sh.

import AppKit

let port = 8377
let baseURL = URL(string: "http://127.0.0.1:\(port)")!
// Set by build.sh to the repo folder that holds app.py.
let projectDir = Bundle.main.object(forInfoDictionaryKey: "ACBProjectDir") as? String ?? ""
let logPath = (projectDir as NSString).appendingPathComponent("server.log")

struct Failure: Error, CustomStringConvertible {
    let description: String
    init(_ d: String) { description = d }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let menu = NSMenu()
    private let work = DispatchQueue(label: "convos.work")
    private var running = false
    private var busy: String?       // label of the action in progress
    private var lastResult: String? // outcome of the last action
    private var server: Process?

    func applicationDidFinishLaunching(_ note: Notification) {
        menu.delegate = self
        statusItem.menu = menu
        updateIcon()
        start()
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.refresh() }
    }

    /// Quitting Convos stops the server too.
    func applicationWillTerminate(_ note: Notification) {
        server?.terminate()
        for pid in serverPIDs() { kill(pid, SIGTERM) }
    }

    // MARK: menu

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let status = busy ?? (running ? "● Running on localhost:\(port)" : "○ Stopped")
        menu.addItem(withTitle: status, action: nil, keyEquivalent: "")
        if let r = lastResult { menu.addItem(withTitle: r, action: nil, keyEquivalent: "") }
        menu.addItem(.separator())
        add("Open in Browser", #selector(openPage), "o")
        if running {
            add("Stop", #selector(stop))
            add("Restart", #selector(restart), "r")
            add("Reindex", #selector(reindex), "i")
        } else {
            add("Start", #selector(start), "s")
        }
        menu.addItem(.separator())
        add("Show Log", #selector(showLog))
        let quit = menu.addItem(withTitle: "Quit Convos", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        quit.target = NSApp
    }

    private func add(_ title: String, _ action: Selector, _ key: String = "") {
        let item = menu.addItem(withTitle: title, action: busy == nil ? action : nil, keyEquivalent: key)
        item.target = self
    }

    private func updateIcon() {
        let name = busy != nil ? "arrow.triangle.2.circlepath"
            : running ? "bubble.left.and.bubble.right.fill" : "bubble.left.and.bubble.right"
        statusItem.button?.image = NSImage(systemSymbolName: name, accessibilityDescription: "Convos")
    }

    // MARK: actions

    /// Opens the page, and starts the server first when it is not running.
    @objc private func openPage() {
        if running { NSWorkspace.shared.open(baseURL); return }
        perform("Starting…") {
            try self.launch()
            DispatchQueue.main.async { NSWorkspace.shared.open(baseURL) }
            return "Started"
        }
    }

    @objc private func start() {
        perform("Starting…") { try self.launch(); return "Started" }
    }

    @objc private func stop() {
        perform("Stopping…") { try self.halt(); return "Stopped" }
    }

    @objc private func restart() {
        perform("Restarting…") { try self.halt(); try self.launch(); return "Restarted" }
    }

    @objc private func reindex() {
        perform("Reindexing…") {
            var req = URLRequest(url: baseURL.appendingPathComponent("api/reindex"), timeoutInterval: 600)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = Data("{}".utf8)
            let (data, code) = self.fetch(req)
            guard code == 200, let data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let stats = json["stats"] as? [String: [String: Any]]
            else { throw Failure("reindex failed (HTTP \(code))") }
            let sum = { (key: String) in stats.values.reduce(0) { $0 + ($1[key] as? Int ?? 0) } }
            return "Reindexed: \(sum("updated")) updated, \(sum("removed")) removed, \(sum("errors")) errors"
        }
    }

    @objc private func showLog() {
        NSWorkspace.shared.open(URL(fileURLWithPath: logPath))
    }

    /// Runs `body` off the main thread with a busy label, then records its result.
    private func perform(_ label: String, _ body: @escaping () throws -> String) {
        busy = label
        updateIcon()
        work.async {
            let result: String
            do { result = try body() } catch { result = "Error: \(error)" }
            let up = self.isUp()
            DispatchQueue.main.async {
                self.busy = nil
                self.running = up
                self.lastResult = "\(result) at \(Self.clock.string(from: Date()))"
                self.updateIcon()
            }
        }
    }

    private static let clock: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()

    // MARK: server control (runs on `work`)

    private func launch() throws {
        if isUp() { return }
        guard FileManager.default.fileExists(atPath: (projectDir as NSString).appendingPathComponent("app.py"))
        else { throw Failure("app.py not found in \(projectDir)") }
        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil)
        }
        guard let log = FileHandle(forWritingAtPath: logPath) else { throw Failure("cannot open server.log") }
        log.seekToEndOfFile()
        // Login shell, so python3 resolves the same way it does in Terminal.
        let p = Process()
        p.executableURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh")
        p.arguments = ["-lc", "exec python3 -u app.py --no-browser --port \(port)"]
        p.currentDirectoryURL = URL(fileURLWithPath: projectDir)
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = log
        p.standardError = log
        try p.run()
        server = p
        // Startup indexes before it serves. A first full index can take a while.
        try waitUntil(up: true, timeout: 300)
    }

    private func halt() throws {
        for pid in serverPIDs() { kill(pid, SIGTERM) }
        try waitUntil(up: false, timeout: 10)
    }

    /// PIDs listening on the port whose command line runs app.py.
    private func serverPIDs() -> [pid_t] {
        run("/usr/sbin/lsof", ["-nP", "-t", "-iTCP:\(port)", "-sTCP:LISTEN"])
            .split(separator: "\n")
            .compactMap { pid_t($0) }
            .filter { run("/bin/ps", ["-o", "command=", "-p", String($0)]).contains("app.py") }
    }

    private func waitUntil(up: Bool, timeout: TimeInterval) throws {
        let deadline = Date().addingTimeInterval(timeout)
        while isUp() != up {
            if up, let p = server, !p.isRunning {
                throw Failure("server exited with code \(p.terminationStatus), see log")
            }
            if Date() > deadline { throw Failure("timed out waiting for server to \(up ? "start" : "stop")") }
            Thread.sleep(forTimeInterval: 0.5)
        }
    }

    /// True when something answers HTTP on the port. The server has no HEAD
    /// handler, so it replies 501 at once, which still proves it is serving.
    private func isUp() -> Bool {
        var req = URLRequest(url: baseURL, timeoutInterval: 1)
        req.httpMethod = "HEAD"
        return fetch(req).code != 0
    }

    private func refresh() {
        guard busy == nil else { return }
        work.async {
            let up = self.isUp()
            DispatchQueue.main.async {
                guard self.busy == nil else { return }
                self.running = up
                self.updateIcon()
            }
        }
    }

    // MARK: helpers

    private func fetch(_ req: URLRequest) -> (data: Data?, code: Int) {
        let done = DispatchSemaphore(value: 0)
        var result: (Data?, Int) = (nil, 0)
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            result = (data, (resp as? HTTPURLResponse)?.statusCode ?? 0)
            done.signal()
        }.resume()
        done.wait()
        return result
    }

    private func run(_ tool: String, _ args: [String]) -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: tool)
        p.arguments = args
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return "" }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(decoding: data, as: UTF8.self)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
